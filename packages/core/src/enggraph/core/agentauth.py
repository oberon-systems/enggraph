r"""Host agents: what proves a request is theirs, and what the stack keeps of them.

An agent registers and gets a one-time claim code; an operator approves it, and
the claim hands over a credential. Every later request is signed:

    X-Credential: <kid>:<generation>:<expires_at>
    X-Timestamp:  <unix seconds>
    X-Signature:  HMAC-SHA256(key, "ts\nMETHOD\npath\nsha256hex(body)")

The key is HMAC-SHA256(master[kid], "agent_id:generation:expires_at"), so the
stack stores no agent key, only the master keys in `agent_master_keys`.
`tests/data/agent_signing_vectors.json` holds the same answers for the Rust agent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import posixpath
import re
import secrets
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from enggraph.core import db
from enggraph.core.identifiers import project_name
from enggraph.core.storage import agent_root

log = logging.getLogger(__name__)

SIGNATURE_WINDOW_SECONDS = 300
CREDENTIAL_SECONDS = 60 * 60
REFRESH_BEFORE_SECONDS = 15 * 60
CLAIM_SECONDS = 24 * 60 * 60
CONNECTED_SECONDS = 60
FINGERPRINT_CHARS = 8
DEFAULT_KID = "k1"
MASTER_KEY_BYTES = 32
# What an agent signs is the path it asks the gateway for, which strips this.
GATEWAY_PREFIX = "/agent"
STATES = ("enabled", "disabled")
DECISIONS = ("approved", "refused")
TREE_TYPES = ("codebase", "docs", "config")
EXPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
NUMBER = re.compile(r"[0-9]{1,12}")

ACCEPTED = "accepted"
PROMOTED = "promoted"
SUPERSEDED = "superseded"
REFUSED = "refused"


class AgentAuthError(Exception):
    """An operator's or an agent's request that cannot be done, and why."""


class WaitingError(Exception):
    """A claim for a registration no operator has decided yet."""


def now() -> int:
    """Return the clock everything here reads, in whole seconds."""
    return int(time.time())


def signed_path(path: str) -> str:
    """Return the path an agent signed for a path this service was asked."""
    return f"{GATEWAY_PREFIX}{path}"


def current_kid() -> str:
    """Return the master key new credentials are issued under."""
    return os.environ.get("AGENT_CURRENT_KID", "").strip() or DEFAULT_KID


def _stored_keys() -> dict[str, str]:
    rows = db.query("SELECT kid, key FROM agent_master_keys")
    return {row["kid"]: row["key"] for row in rows}


def master_keys() -> dict[str, str]:
    """Return every master key, making the current one the first time it is needed."""
    keys = _stored_keys()
    if current_kid() in keys:
        return keys
    db.run(
        """INSERT INTO agent_master_keys (kid, key) VALUES ($1, $2)
           ON CONFLICT (kid) DO NOTHING""",
        [current_kid(), secrets.token_hex(MASTER_KEY_BYTES)],
    )
    return _stored_keys()


def signing_keys() -> tuple[str, dict[str, str]]:
    """Return the current kid and the keys."""
    return current_kid(), master_keys()


def digest(text: str) -> str:
    """Return the sha256 a claim code is stored as."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(code: str) -> str:
    """Return what the agent logs and the dashboard shows of a claim code."""
    return digest(code)[:FINGERPRINT_CHARS]


def claim_signature(code: str, body: bytes) -> str:
    """Return the HMAC under the claim code that a claim answer carries."""
    return hmac.new(code.encode("utf-8"), body, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Credential:
    """What an agent presents with every request; none of it is secret."""

    owner_id: str
    kid: str
    generation: int
    expires_at: int

    @classmethod
    def parse(cls, owner_id: str, header: str) -> Credential | None:
        """Read an X-Credential header, or None when it is not one."""
        rest, _, expires_at = header.rpartition(":")
        kid, _, generation = rest.rpartition(":")
        if kid == "" or not NUMBER.fullmatch(generation):
            return None
        if not NUMBER.fullmatch(expires_at):
            return None
        return cls(owner_id, kid, int(generation), int(expires_at))

    @property
    def header(self) -> str:
        """Return the X-Credential form."""
        return f"{self.kid}:{self.generation}:{self.expires_at}"

    def key(self, keys: Mapping[str, str]) -> str | None:
        """Derive the agent's key under the master key the credential names."""
        master = keys.get(self.kid)
        if master is None:
            return None
        message = f"{self.owner_id}:{self.generation}:{self.expires_at}"
        return hmac.new(
            master.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def sign(
        self,
        keys: Mapping[str, str],
        method: str,
        path: str,
        body: bytes,
        timestamp: int,
    ) -> str | None:
        r"""Return HMAC-SHA256 of `ts\nMETHOD\npath\nsha256hex(body)`."""
        key = self.key(keys)
        if key is None:
            return None
        message = "\n".join(
            (str(timestamp), method.upper(), path, hashlib.sha256(body).hexdigest())
        )
        return hmac.new(
            key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest()


def weigh(
    credential: Credential,
    generation: int,
    pending_generation: int | None,
    keys: Mapping[str, str],
    request: tuple[str, str, bytes, int, str],
    at: int | None = None,
) -> str:
    """Judge a signed request: accepted, promoted, superseded or refused.

    `request` is the method, the signed path, the body, the timestamp and the
    signature. A superseded generation that still signs correctly means two
    parties hold it, which is why only that verdict disables the agent.
    """
    method, path, body, timestamp, signature = request
    moment = now() if at is None else at
    if abs(moment - timestamp) > SIGNATURE_WINDOW_SECONDS:
        return REFUSED
    expected = credential.sign(keys, method, path, body, timestamp)
    if expected is None or moment >= credential.expires_at:
        return REFUSED
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        return REFUSED
    if credential.generation == generation:
        return ACCEPTED
    if credential.generation == pending_generation:
        return PROMOTED
    if credential.generation < generation:
        return SUPERSEDED
    return REFUSED


def verify_request(
    agent_id: str,
    credential_header: str,
    timestamp: str,
    signature: str,
    method: str,
    path: str,
    body: bytes,
) -> Credential | None:
    """Return the credential of a good signed request by an enabled agent."""
    credential = Credential.parse(agent_id, credential_header)
    if credential is None or not NUMBER.fullmatch(timestamp):
        return None
    keys = master_keys()
    rows = db.query(
        """SELECT a.state, s.generation, s.pending_generation
         FROM agents AS a
         JOIN agent_secrets AS s ON s.agent_id = a.id
        WHERE a.id = $1""",
        [agent_id],
    )
    if not rows or rows[0]["state"] != "enabled":
        return None
    verdict = weigh(
        credential,
        rows[0]["generation"],
        rows[0]["pending_generation"],
        keys,
        (method, path, body, int(timestamp), signature),
    )
    if verdict == PROMOTED:
        db.query(
            """UPDATE agent_secrets
            SET generation = $2::int, pending_generation = NULL
          WHERE agent_id = $1 AND pending_generation = $2::int""",
            [agent_id, credential.generation],
        )
    if verdict == SUPERSEDED:
        log.warning("agent %s signed with a superseded generation", agent_id)
        db.query("UPDATE agents SET state = 'disabled' WHERE id = $1", [agent_id])
    return credential if verdict in (ACCEPTED, PROMOTED) else None


def clean_exports(exports: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """Hold announced exports to a name and one canonical absolute path each."""
    cleaned: dict[str, dict[str, str]] = {}
    for entry in exports:
        name, path = entry.get("name", ""), entry.get("path", "")
        if not EXPORT_NAME.fullmatch(name):
            raise AgentAuthError(f"{name!r} is not an export name")
        canonical = path.startswith("/") and not path.startswith("//")
        if not canonical or posixpath.normpath(path) != path or "\0" in path:
            raise AgentAuthError(f"export {name}: {path!r} is not a canonical path")
        if name in cleaned:
            raise AgentAuthError(f"export {name} is announced twice")
        cleaned[name] = {"name": name, "path": path}
    return list(cleaned.values())


def _prune(tx: db.Session) -> None:
    """Forget lapsed registrations, and the agents that were nothing but one."""
    tx.run("DELETE FROM agent_registrations WHERE expires_at <= now()")
    tx.run(
        """DELETE FROM agents AS a
        WHERE a.state = 'pending'
          AND NOT EXISTS (SELECT 1 FROM agent_registrations AS r
                           WHERE r.agent_id = a.id)"""
    )


def register(
    name: str,
    labels: list[str],
    agent_id: str | None,
    exports: Iterable[Mapping[str, str]],
) -> str:
    """Queue a registration for an operator and return its one-time claim code.

    A known id waits like a new one, and changes nothing about the agent until
    an operator approves it; a registration already waiting is replaced.
    """
    announced = clean_exports(exports)
    owner = agent_id or str(uuid.uuid4())
    code = secrets.token_urlsafe(32)
    with db.transaction() as tx:
        _prune(tx)
        tx.run(
            """INSERT INTO agents (id, name, labels) VALUES ($1, $2, $3::text[])
           ON CONFLICT (id) DO NOTHING""",
            [owner, name, labels],
        )
        tx.run(
            """INSERT INTO agent_registrations
                  (agent_id, name, labels, exports, claim_sha256, expires_at)
           VALUES ($1, $2, $3::text[], $4::jsonb, $5,
                   now() + make_interval(secs => $6::int))
           ON CONFLICT (agent_id) DO UPDATE
              SET name = EXCLUDED.name,
                  labels = EXCLUDED.labels,
                  exports = EXCLUDED.exports,
                  claim_sha256 = EXCLUDED.claim_sha256,
                  registered_at = now(),
                  expires_at = EXCLUDED.expires_at,
                  approved_at = NULL""",
            [owner, name, labels, json.dumps(announced), digest(code), CLAIM_SECONDS],
        )
    return code


def _issue(
    tx: db.Session, agent_id: str, kid: str, keys: Mapping[str, str]
) -> tuple[Credential, str]:
    """Open generation 1, or hand out a fresh pending generation after it."""
    rows = tx.query(
        """INSERT INTO agent_secrets (agent_id, kid, generation)
       VALUES ($1, $2, 1)
       ON CONFLICT (agent_id) DO UPDATE
          SET kid = EXCLUDED.kid,
              pending_generation = GREATEST(
                  agent_secrets.generation,
                  COALESCE(agent_secrets.pending_generation, 0)) + 1
    RETURNING generation, pending_generation""",
        [agent_id, kid],
    )
    generation = rows[0]["pending_generation"] or rows[0]["generation"]
    credential = Credential(agent_id, kid, generation, now() + CREDENTIAL_SECONDS)
    key = credential.key(keys)
    if key is None:
        raise RuntimeError(f"no master key {kid!r}")
    return credential, key


def claim(code: str) -> tuple[Credential, str] | None:
    """Spend an approved claim code on a credential; None when it is no longer one.

    Raises WaitingError while no operator has decided. The row is taken under a
    lock and deleted, so of two racing claims only the first gets a credential.
    """
    with db.transaction() as tx:
        _prune(tx)
        rows = tx.query(
            """SELECT agent_id, approved_at FROM agent_registrations
          WHERE claim_sha256 = $1 FOR UPDATE""",
            [digest(code)],
        )
        if not rows:
            return None
        if rows[0]["approved_at"] is None:
            raise WaitingError(f"agent {rows[0]['agent_id']} waits for an operator")
        kid, keys = signing_keys()
        tx.run(
            "DELETE FROM agent_registrations WHERE agent_id = $1",
            [rows[0]["agent_id"]],
        )
        return _issue(tx, rows[0]["agent_id"], kid, keys)


def refresh(agent_id: str) -> tuple[Credential, str]:
    """Hand a signed agent a fresh credential in a pending generation."""
    kid, keys = signing_keys()
    with db.transaction() as tx:
        return _issue(tx, agent_id, kid, keys)


def _drop_bindings(tx: db.Session, agent_id: str, export: str) -> None:
    """Take an export's binding back: the project keeps its graph, unread."""
    rows, _ = tx.run(
        """DELETE FROM project_sources WHERE agent_id = $1 AND export = $2
       RETURNING project""",
        [agent_id, export],
    )
    for row in rows:
        log.warning("project %s lost its export %s", row["project"], export)


def _announce(
    tx: db.Session, agent_id: str, announced: Iterable[Mapping[str, str]]
) -> None:
    """Bring an agent's export rows to the list it announced.

    A new name or a changed path waits for an operator again, and a binding
    never survives its path changing under it.
    """
    wanted = {entry["name"]: entry["path"] for entry in announced}
    held = {
        row["name"]: row["host_path"]
        for row in tx.query(
            "SELECT name, host_path FROM agent_exports WHERE agent_id = $1",
            [agent_id],
        )
    }
    for name in held.keys() - wanted.keys():
        tx.run(
            "DELETE FROM agent_exports WHERE agent_id = $1 AND name = $2",
            [agent_id, name],
        )
        _drop_bindings(tx, agent_id, name)
    for name, path in wanted.items():
        if name not in held:
            tx.run(
                """INSERT INTO agent_exports (agent_id, name, host_path)
               VALUES ($1, $2, $3)""",
                [agent_id, name, path],
            )
        elif held[name] != path:
            tx.run(
                """UPDATE agent_exports
                SET host_path = $3, state = 'pending', announced_at = now(),
                    decided_at = NULL, decided_by = NULL
              WHERE agent_id = $1 AND name = $2""",
                [agent_id, name, path],
            )
            _drop_bindings(tx, agent_id, name)


def set_exports(agent_id: str, exports: Iterable[Mapping[str, str]]) -> list[db.Row]:
    """Store what a signed agent announces, and answer where each one stands."""
    announced = clean_exports(exports)
    with db.transaction() as tx:
        if not tx.query("SELECT 1 FROM agents WHERE id = $1 FOR KEY SHARE", [agent_id]):
            raise AgentAuthError(f"no agent {agent_id}")
        _announce(tx, agent_id, announced)
        return tx.query(
            """SELECT e.name, e.host_path, e.state, s.project AS bound_to
           FROM agent_exports AS e
           LEFT JOIN project_sources AS s
             ON s.agent_id = e.agent_id AND s.export = e.name
          WHERE e.agent_id = $1 ORDER BY e.name""",
            [agent_id],
        )


def seen(credential: Credential, version: str | None = None) -> bool:
    """Note a live socket, and say whether its credential still stands."""
    rows = db.query(
        """UPDATE agents AS a
        SET last_seen_at = now(), version = COALESCE($2, a.version)
       FROM agent_secrets AS s
      WHERE a.id = $1 AND s.agent_id = a.id
  RETURNING a.state, s.generation""",
        [credential.owner_id, version],
    )
    return _stands(credential, rows)


def standing(credential: Credential) -> bool:
    """Say whether a socket's credential still stands, noting nothing."""
    rows = db.query(
        """SELECT a.state, s.generation
         FROM agents AS a
         JOIN agent_secrets AS s ON s.agent_id = a.id
        WHERE a.id = $1""",
        [credential.owner_id],
    )
    return _stands(credential, rows)


def _stands(credential: Credential, rows: list[db.Row]) -> bool:
    return (
        bool(rows)
        and rows[0]["state"] == "enabled"
        and credential.generation >= rows[0]["generation"]
    )


def list_registrations() -> list[db.Row]:
    """Return what waits for an operator, oldest first; the claim hash stays here."""
    with db.transaction() as tx:
        _prune(tx)
        return tx.query(
            """SELECT r.agent_id, r.name, r.labels, r.exports, r.registered_at,
                r.expires_at, r.approved_at, a.state AS agent_state,
                left(r.claim_sha256, $1::int) AS fingerprint
           FROM agent_registrations AS r
           LEFT JOIN agents AS a ON a.id = r.agent_id
          ORDER BY r.registered_at, r.agent_id""",
            [FINGERPRINT_CHARS],
        )


def list_agents() -> list[db.Row]:
    """Return every agent an operator approved once, by name."""
    return db.query(
        """SELECT a.id, a.name, a.labels, a.state, a.version, a.created_at,
              a.approved_at, a.approved_by, a.last_seen_at, s.generation,
              COALESCE(a.last_seen_at > now() - make_interval(secs => $1::int),
                       false) AS connected
         FROM agents AS a
         LEFT JOIN agent_secrets AS s ON s.agent_id = a.id
        WHERE a.state <> 'pending'
        ORDER BY a.name, a.id""",
        [CONNECTED_SECONDS],
    )


def list_exports() -> list[db.Row]:
    """Return every export, with its binding and the project it could bind to."""
    return db.query(
        """SELECT e.agent_id, e.name, e.host_path, e.state, e.announced_at,
              e.decided_at, e.decided_by, s.project AS bound_to,
              p.name AS same_path
         FROM agent_exports AS e
         LEFT JOIN project_sources AS s
           ON s.agent_id = e.agent_id AND s.export = e.name
         LEFT JOIN projects AS p
           ON p.root_path = e.host_path
          AND NOT EXISTS (SELECT 1 FROM project_sources AS o
                           WHERE o.project = p.name)
        ORDER BY e.agent_id, e.name"""
    )


def approve_registration(agent_id: str, by: str) -> bool:
    """Let a waiting agent claim; a disabled one stays so until it is enabled."""
    with db.transaction() as tx:
        _prune(tx)
        rows = tx.query(
            """UPDATE agent_registrations SET approved_at = now()
          WHERE agent_id = $1 AND approved_at IS NULL
      RETURNING name, labels, exports""",
            [agent_id],
        )
        if not rows:
            return False
        tx.run(
            """UPDATE agents
            SET name = $2, labels = $3::text[], approved_at = now(), approved_by = $4,
                state = CASE WHEN state = 'pending' THEN 'enabled' ELSE state END
          WHERE id = $1""",
            [agent_id, rows[0]["name"], rows[0]["labels"], by],
        )
        _announce(tx, agent_id, rows[0]["exports"])
    return True


def refuse_registration(agent_id: str) -> bool:
    """Forget a registration, so its claim code opens nothing."""
    with db.transaction() as tx:
        _, touched = tx.run(
            "DELETE FROM agent_registrations WHERE agent_id = $1", [agent_id]
        )
        tx.run("DELETE FROM agents WHERE id = $1 AND state = 'pending'", [agent_id])
    return touched > 0


def set_state(agent_id: str, state: str) -> bool:
    """Enable or disable an approved agent; False when there is none."""
    if state not in STATES:
        raise AgentAuthError(f"state must be one of {', '.join(STATES)}")
    _, touched = db.run(
        "UPDATE agents SET state = $2 WHERE id = $1 AND state <> 'pending'",
        [agent_id, state],
    )
    return touched > 0


def revoke(agent_id: str) -> bool:
    """Bump the generation, so the credential out there is superseded at once."""
    with db.transaction() as tx:
        _, bumped = tx.run(
            """UPDATE agent_secrets
            SET generation = GREATEST(generation,
                                      COALESCE(pending_generation, 0)) + 1,
                pending_generation = NULL
          WHERE agent_id = $1""",
            [agent_id],
        )
        _, unclaimed = tx.run(
            """DELETE FROM agent_registrations
          WHERE agent_id = $1 AND approved_at IS NOT NULL""",
            [agent_id],
        )
    return bumped + unclaimed > 0


def delete_agent(agent_id: str) -> bool:
    """Forget an agent and what it offered; the projects it served stay as they are."""
    with db.transaction() as tx:
        _, deleted = tx.run("DELETE FROM agents WHERE id = $1", [agent_id])
        tx.run("DELETE FROM agent_secrets WHERE agent_id = $1", [agent_id])
        tx.run("DELETE FROM agent_registrations WHERE agent_id = $1", [agent_id])
        tx.run("DELETE FROM agent_exports WHERE agent_id = $1", [agent_id])
        tx.run("DELETE FROM project_sources WHERE agent_id = $1", [agent_id])
    return deleted > 0


def decide_export(agent_id: str, export: str, decision: str, by: str) -> bool:
    """Approve or refuse an export; refusing one takes its binding back."""
    if decision not in DECISIONS:
        raise AgentAuthError(f"decision must be one of {', '.join(DECISIONS)}")
    with db.transaction() as tx:
        _, touched = tx.run(
            """UPDATE agent_exports
            SET state = $3, decided_at = now(), decided_by = $4
          WHERE agent_id = $1 AND name = $2""",
            [agent_id, export, decision, by],
        )
        if touched and decision == "refused":
            _drop_bindings(tx, agent_id, export)
    return touched > 0


def _approved_export(tx: db.Session, agent_id: str, export: str) -> db.Row:
    """Return an approved export and its agent's name, locked against a change."""
    rows = tx.query(
        """SELECT e.host_path, e.state, a.name AS agent_name
         FROM agent_exports AS e
         JOIN agents AS a ON a.id = e.agent_id
        WHERE e.agent_id = $1 AND e.name = $2
          FOR KEY SHARE""",
        [agent_id, export],
    )
    if not rows:
        raise AgentAuthError(f"agent {agent_id} announces no export {export}")
    if rows[0]["state"] != "approved":
        raise AgentAuthError(f"export {export} is {rows[0]['state']}, not approved")
    return rows[0]


def _existing_project(tx: db.Session, project: str, host_path: str) -> None:
    """Refuse a project that does not read the directory the export is."""
    rows = tx.query(
        "SELECT root_path FROM projects WHERE name = $1 FOR KEY SHARE", [project]
    )
    if not rows:
        raise AgentAuthError(f"no project named {project}")
    if rows[0]["root_path"] != host_path:
        raise AgentAuthError(
            f"project {project} reads {rows[0]['root_path']}, not {host_path}: "
            "an export binds to an existing project only on the same path"
        )


def _new_project(tx: db.Session, export: str, source: db.Row, project_type: str) -> str:
    """Register a project named after an export, and return its name."""
    if project_type not in TREE_TYPES:
        raise AgentAuthError(f"type must be one of {', '.join(TREE_TYPES)}")
    try:
        project = project_name(export, "")
    except RuntimeError as error:
        raise AgentAuthError(str(error)) from error
    if tx.query("SELECT 1 FROM projects WHERE name = $1", [project]):
        raise AgentAuthError(
            f"project {project} already exists on another path; "
            "rename the export on its host to bind it as a new project"
        )
    host_path = source["host_path"]
    root = host_path
    if tx.query("SELECT 1 FROM projects WHERE root_path = $1", [root]):
        root = agent_root(source["agent_name"], host_path)
    if tx.query("SELECT 1 FROM projects WHERE root_path = $1", [root]):
        raise AgentAuthError(f"{root} is already another project's root")
    tx.run(
        "INSERT INTO projects (name, root_path, type) VALUES ($1, $2, $3)",
        [project, root, project_type],
    )
    return project


def _bind(
    tx: db.Session,
    agent_id: str,
    export: str,
    by: str,
    project: str | None,
    project_type: str,
) -> db.Row:
    source = _approved_export(tx, agent_id, export)
    if tx.query(
        "SELECT project FROM project_sources WHERE agent_id = $1 AND export = $2",
        [agent_id, export],
    ):
        raise AgentAuthError(f"export {export} is already bound")
    if project is None:
        bound = _new_project(tx, export, source, project_type)
    else:
        _existing_project(tx, project, source["host_path"])
        bound = project
    if tx.query("SELECT 1 FROM project_sources WHERE project = $1", [bound]):
        raise AgentAuthError(f"project {bound} is already bound to an export")
    return tx.query(
        """INSERT INTO project_sources
              (project, agent_id, export, host_path, bound_by)
       VALUES ($1, $2, $3, $4, $5)
    RETURNING project, agent_id, export, host_path, bound_at, bound_by""",
        [bound, agent_id, export, source["host_path"], by],
    )[0]


def bind_export(
    agent_id: str,
    export: str,
    by: str,
    project: str | None = None,
    project_type: str = TREE_TYPES[0],
) -> db.Row:
    """Bind an approved export to a project; nothing reads through it yet."""
    with db.transaction() as tx:
        return _bind(tx, agent_id, export, by, project, project_type)


def importable(under: Iterable[str] = ()) -> list[db.Row]:
    """Return the projects read from a host directory, under the given ones if any."""
    roots = [root.rstrip("/") for root in under]
    rows = db.query(
        """SELECT p.name, p.root_path, s.project AS bound_to
         FROM projects AS p
         LEFT JOIN project_sources AS s ON s.project = p.name
        WHERE p.root_path LIKE '/%'
        ORDER BY p.name"""
    )
    if not roots:
        return rows
    return [
        row
        for row in rows
        if any(
            row["root_path"] == root or row["root_path"].startswith(f"{root}/")
            for root in roots
        )
    ]


def approve_import(agent_id: str, by: str) -> dict[str, list[dict[str, str]]]:
    """Approve and bind every export of an agent on an unbound project's own path.

    An export an operator refused stays refused. The rest binds one by one, and
    what cannot bind is named with its reason rather than stopping the others.
    """
    bound: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    with db.transaction() as tx:
        rows = tx.query(
            """SELECT e.name, e.state, p.name AS project
             FROM agent_exports AS e
             LEFT JOIN project_sources AS s
               ON s.agent_id = e.agent_id AND s.export = e.name
             LEFT JOIN projects AS p
               ON p.root_path = e.host_path
              AND NOT EXISTS (SELECT 1 FROM project_sources AS o
                               WHERE o.project = p.name)
            WHERE e.agent_id = $1 AND s.project IS NULL
            ORDER BY e.name""",
            [agent_id],
        )
        for row in rows:
            export = row["name"]
            if row["project"] is None:
                reason = "no unbound project reads this path"
                skipped.append({"export": export, "reason": reason})
                continue
            if row["state"] == "refused":
                skipped.append({"export": export, "reason": "an operator refused it"})
                continue
            if row["project"] in {entry["project"] for entry in bound}:
                reason = f"project {row['project']} is bound to another of this path"
                skipped.append({"export": export, "reason": reason})
                continue
            tx.run(
                """UPDATE agent_exports
                SET state = 'approved', decided_at = now(), decided_by = $3
              WHERE agent_id = $1 AND name = $2 AND state <> 'approved'""",
                [agent_id, export, by],
            )
            try:
                _bind(tx, agent_id, export, by, row["project"], TREE_TYPES[0])
            except AgentAuthError as error:
                skipped.append({"export": export, "reason": str(error)})
                continue
            bound.append({"export": export, "project": row["project"]})
    return {"bound": bound, "skipped": skipped}


def unbind(project: str) -> bool:
    """Take a project's binding back; the project itself stays as it is."""
    _, touched = db.run("DELETE FROM project_sources WHERE project = $1", [project])
    return touched > 0


def known_agent(agent_id: str) -> bool:
    """Say whether an id names an approved agent a token may be bound to."""
    return bool(
        db.query(
            "SELECT 1 FROM agents WHERE id = $1 AND state <> 'pending'", [agent_id]
        )
    )
