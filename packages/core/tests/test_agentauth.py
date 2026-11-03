"""Host agent credentials: the shared vectors, the verdicts, and what is stored."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from enggraph.core import agentauth, db, storage
from enggraph.core.agentauth import Credential

DATABASE_URL = os.environ.get("EVAL_DATABASE_URL")
VECTORS = json.loads(
    (Path(__file__).parent / "data" / "agent_signing_vectors.json").read_text(
        encoding="utf-8"
    )
)
MASTER = VECTORS["master_keys"]
AGENT = "11111111-2222-4333-8444-555555555555"
NOW = 1_900_000_000
ON_DATABASE = pytest.mark.skipif(
    DATABASE_URL is None, reason="EVAL_DATABASE_URL is not set"
)


def request(
    credential: Credential, at: int = NOW, body: bytes = b"{}"
) -> tuple[str, str, bytes, int, str]:
    """Return a correctly signed request by a credential."""
    signature = credential.sign(MASTER, "PUT", "/agent/x", body, at)
    assert signature is not None
    return "PUT", "/agent/x", body, at, signature


@pytest.mark.parametrize("case", VECTORS["signing"])
def test_the_signing_vectors_hold(case: dict[str, Any]) -> None:
    """The Rust agent reads the same file, so the two cannot drift apart."""
    credential = Credential.parse(case["agent_id"], case["credential"])
    assert credential is not None
    assert credential.header == case["credential"]
    assert credential.key(MASTER) == case["key"]
    assert (
        credential.sign(
            MASTER,
            case["method"],
            case["path"],
            case["body"].encode("utf-8"),
            case["timestamp"],
        )
        == case["signature"]
    )


@pytest.mark.parametrize("case", VECTORS["claim"])
def test_the_claim_vectors_hold(case: dict[str, str]) -> None:
    """A claim answer is signed under the code, and only its fingerprint is shown."""
    body = case["body"].encode("utf-8")
    assert agentauth.claim_signature(case["code"], body) == case["signature"]
    assert agentauth.fingerprint(case["code"]) == case["fingerprint"]


@pytest.mark.parametrize(
    "header", ["", "k1", "k1:1", ":1:2", "k1:x:2", "k1:1:y", "k1:-1:2", "k1:1:2:"]
)
def test_a_malformed_credential_is_none(header: str) -> None:
    """A header that does not parse never reaches the signature check."""
    assert Credential.parse(AGENT, header) is None


def test_a_good_signature_on_the_current_generation_is_accepted() -> None:
    """The ordinary request."""
    credential = Credential(AGENT, "k1", 3, NOW + 60)
    verdict = agentauth.weigh(credential, 3, None, MASTER, request(credential), NOW)
    assert verdict == agentauth.ACCEPTED


def test_the_window_and_the_expiry_refuse() -> None:
    """A replay out of the window and a credential past its life are both refused."""
    credential = Credential(AGENT, "k1", 3, NOW + 600)
    stale = request(credential, NOW - agentauth.SIGNATURE_WINDOW_SECONDS - 1)
    assert agentauth.weigh(credential, 3, None, MASTER, stale, NOW) == agentauth.REFUSED
    edge = request(credential, NOW - agentauth.SIGNATURE_WINDOW_SECONDS)
    assert agentauth.weigh(credential, 3, None, MASTER, edge, NOW) == agentauth.ACCEPTED
    spent = Credential(AGENT, "k1", 3, NOW)
    assert (
        agentauth.weigh(spent, 3, None, MASTER, request(spent), NOW)
        == agentauth.REFUSED
    )


def test_a_wrong_signature_or_master_key_refuses() -> None:
    """Without the key nothing else about the credential is believed."""
    credential = Credential(AGENT, "k1", 3, NOW + 60)
    method, path, body, at, signature = request(credential)
    tampered = (method, path, b"{ }", at, signature)
    assert (
        agentauth.weigh(credential, 3, None, MASTER, tampered, NOW) == agentauth.REFUSED
    )
    forged = (method, path, body, at, "0" * 64)
    assert (
        agentauth.weigh(credential, 3, None, MASTER, forged, NOW) == agentauth.REFUSED
    )
    non_ascii = (method, path, body, at, chr(233))
    assert (
        agentauth.weigh(credential, 3, None, MASTER, non_ascii, NOW)
        == agentauth.REFUSED
    )
    other = Credential(AGENT, "k9", 3, NOW + 60)
    assert (
        agentauth.weigh(other, 3, None, MASTER, request(credential), NOW)
        == agentauth.REFUSED
    )


def test_generations_promote_supersede_and_refuse() -> None:
    """Pending is promoted, an older one superseded, a future one simply refused."""
    current = Credential(AGENT, "k1", 3, NOW + 60)
    verdict = agentauth.weigh(current, 2, 3, MASTER, request(current), NOW)
    assert verdict == agentauth.PROMOTED
    verdict = agentauth.weigh(current, 4, None, MASTER, request(current), NOW)
    assert verdict == agentauth.SUPERSEDED
    verdict = agentauth.weigh(current, 2, 4, MASTER, request(current), NOW)
    assert verdict == agentauth.REFUSED


def test_a_badly_signed_old_generation_supersedes_nothing() -> None:
    """Anyone can name an old generation; only one who signs it disables the agent."""
    credential = Credential(AGENT, "k1", 1, NOW + 60)
    method, path, body, at, _ = request(credential)
    forged = (method, path, body, at, "0" * 64)
    assert (
        agentauth.weigh(credential, 5, None, MASTER, forged, NOW) == agentauth.REFUSED
    )


def test_the_master_key_file_is_read_line_by_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys rotate by adding a line; a missing file is no keys, never a crash."""
    keys = tmp_path / "keys"
    keys.write_text("# rotated\nk1=one\n\nk2=two=three\nbroken\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MASTER_KEYS_FILE", str(keys))
    monkeypatch.setenv("AGENT_CURRENT_KID", "k2")
    assert agentauth.master_keys() == {"k1": "one", "k2": "two=three"}
    assert agentauth.signing_keys()[0] == "k2"
    monkeypatch.setenv("AGENT_MASTER_KEYS_FILE", str(tmp_path / "missing"))
    assert agentauth.master_keys() == {}
    with pytest.raises(agentauth.MasterKeyError):
        agentauth.signing_keys()


@pytest.mark.parametrize(
    "exports",
    [
        [{"name": "../alpha", "path": "/srv/alpha"}],
        [{"name": "alpha", "path": "srv/alpha"}],
        [{"name": "alpha", "path": "/srv/alpha/"}],
        [{"name": "alpha", "path": "/srv/../etc"}],
        [{"name": "alpha", "path": "//srv/alpha"}],
        [{"name": "alpha", "path": "/srv/al\0pha"}],
        [{"name": "alpha", "path": "/a"}, {"name": "alpha", "path": "/b"}],
    ],
)
def test_an_export_needs_a_name_and_one_canonical_path(
    exports: list[dict[str, str]],
) -> None:
    """The path is compared with a project's root, so it must be the agent's own."""
    with pytest.raises(agentauth.AgentAuthError):
        agentauth.clean_exports(exports)


def test_an_agent_root_is_nothing_to_mount() -> None:
    """Only the agent reads such a root; a mount of it would be an empty directory."""
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("alpha", "/srv/alpha"),
        ("beta", storage.agent_root("gamma", "/srv/alpha")),
        ("delta", storage.registered_root("delta")),
    ]
    assert storage.list_mountable_projects(cursor) == [("alpha", "/srv/alpha")]


@pytest.fixture
def database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, str]]:
    """Point the pool at the throwaway database, with a master key to sign under."""
    keys = tmp_path / "keys"
    keys.write_text(f"k1={MASTER['k1']}\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("AGENT_MASTER_KEYS_FILE", str(keys))
    monkeypatch.setenv("AGENT_CURRENT_KID", "k1")
    db.close()
    yield {"k1": MASTER["k1"]}
    for table in ("agents", "agent_secrets", "agent_registrations", "agent_exports"):
        db.query(f"DELETE FROM {table}")  # noqa: S608
    db.query("DELETE FROM project_sources WHERE project LIKE 'test-%'")
    db.query("DELETE FROM projects WHERE name LIKE 'test-%'")
    db.close()


def unique(prefix: str = "test-alpha") -> str:
    """Return a name no earlier run left behind."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def enrolled(
    exports: list[dict[str, str]] | None = None, name: str = "test-host"
) -> tuple[Credential, str]:
    """Take an agent through registering, approval and the claim."""
    code = agentauth.register(name, [], None, exports or [])
    with pytest.raises(agentauth.WaitingError):
        agentauth.claim(code)
    agent_id = agentauth.list_registrations()[0]["agent_id"]
    assert agentauth.approve_registration(agent_id, "token:o")
    claimed = agentauth.claim(code)
    assert claimed is not None
    return claimed


def verify(credential: Credential) -> Credential | None:
    """Sign a request with a credential and put it to the stack."""
    at = agentauth.now()
    signature = credential.sign(MASTER, "POST", "/agent/x", b"", at)
    assert signature is not None
    return agentauth.verify_request(
        credential.owner_id,
        credential.header,
        str(at),
        signature,
        "POST",
        "/agent/x",
        b"",
    )


@ON_DATABASE
def test_a_claim_is_spent_once(database: dict[str, str]) -> None:
    """Against the schema: the second claim of one code gets nothing."""
    code = agentauth.register("test-host", ["alpha"], None, [])
    row = agentauth.list_registrations()[0]
    assert row["fingerprint"] == agentauth.fingerprint(code)
    assert "claim_sha256" not in row
    assert agentauth.approve_registration(row["agent_id"], "token:o")
    claimed = agentauth.claim(code)
    assert claimed is not None and claimed[0].generation == 1
    assert agentauth.claim(code) is None
    assert verify(claimed[0]) == claimed[0]


@ON_DATABASE
def test_a_refused_or_lapsed_registration_opens_nothing(
    database: dict[str, str],
) -> None:
    """A refusal leaves no agent behind, and neither does a lapsed claim."""
    code = agentauth.register("test-host", [], None, [])
    agent_id = agentauth.list_registrations()[0]["agent_id"]
    assert agentauth.refuse_registration(agent_id)
    assert agentauth.claim(code) is None
    assert not db.query("SELECT 1 FROM agents WHERE id = $1", [agent_id])

    code = agentauth.register("test-host", [], None, [])
    agent_id = agentauth.list_registrations()[0]["agent_id"]
    db.query("UPDATE agent_registrations SET expires_at = now() - interval '1 second'")
    assert agentauth.claim(code) is None
    assert agentauth.list_registrations() == []
    assert not db.query("SELECT 1 FROM agents WHERE id = $1", [agent_id])


@ON_DATABASE
def test_rotation_promotes_and_a_superseded_generation_disables(
    database: dict[str, str],
) -> None:
    """The old credential works until the new one is used, and is a theft after."""
    first, _ = enrolled()
    second, _ = agentauth.refresh(first.owner_id)
    assert second.generation == 2
    assert verify(first) == first
    assert verify(second) == second
    assert verify(second) == second
    assert verify(first) is None
    assert agentauth.list_agents()[0]["state"] == "disabled"
    assert verify(second) is None


@ON_DATABASE
def test_re_registering_changes_nothing_until_approved(
    database: dict[str, str],
) -> None:
    """A known id may ask again, but a live credential is untouched by the asking."""
    first, _ = enrolled()
    code = agentauth.register("test-other", [], first.owner_id, [])
    assert verify(first) == first
    assert agentauth.list_agents()[0]["name"] == "test-host"
    with pytest.raises(agentauth.WaitingError):
        agentauth.claim(code)
    assert agentauth.approve_registration(first.owner_id, "token:o")
    claimed = agentauth.claim(code)
    assert claimed is not None and claimed[0].generation == 2
    assert agentauth.list_agents()[0]["name"] == "test-other"


@ON_DATABASE
def test_only_enable_brings_a_superseded_agent_back(
    database: dict[str, str],
) -> None:
    """Approving a new claim leaves it disabled: two parties held its credential."""
    first, _ = enrolled()
    assert agentauth.revoke(first.owner_id)
    assert verify(first) is None
    assert agentauth.list_agents()[0]["state"] == "disabled"
    code = agentauth.register("test-host", [], first.owner_id, [])
    assert agentauth.approve_registration(first.owner_id, "token:o")
    claimed = agentauth.claim(code)
    assert claimed is not None
    assert agentauth.list_agents()[0]["state"] == "disabled"
    assert verify(claimed[0]) is None
    assert agentauth.set_state(first.owner_id, "enabled")
    assert verify(claimed[0]) == claimed[0]


@ON_DATABASE
def test_a_socket_stands_until_it_is_cut_off(database: dict[str, str]) -> None:
    """Disabling and revoking are both seen at the next ping."""
    credential, _ = enrolled()
    assert agentauth.seen(credential, "1.2.3")
    assert agentauth.list_agents()[0]["version"] == "1.2.3"
    assert agentauth.list_agents()[0]["connected"] is True
    assert agentauth.standing(credential)
    agentauth.set_state(credential.owner_id, "disabled")
    assert not agentauth.seen(credential)
    agentauth.set_state(credential.owner_id, "enabled")
    assert agentauth.standing(credential)
    agentauth.revoke(credential.owner_id)
    assert not agentauth.standing(credential)


@ON_DATABASE
def test_exports_wait_and_a_changed_path_loses_its_binding(
    database: dict[str, str],
) -> None:
    """A new name or path is pending again; a removed export takes its binding."""
    name = unique()
    alpha = {"name": name, "path": f"/srv/{name}"}
    credential, _ = enrolled([alpha, {"name": "test-beta", "path": "/srv/beta"}])
    agent = credential.owner_id
    held = {row["name"]: row for row in agentauth.list_exports()}
    assert {row["state"] for row in held.values()} == {"pending"}
    with pytest.raises(agentauth.AgentAuthError):
        agentauth.bind_export(agent, name, "token:o")
    assert agentauth.decide_export(agent, name, "approved", "token:o")
    bound = agentauth.bind_export(agent, name, "token:o")
    assert bound["project"] == name and bound["verified_at"] is None

    answered = agentauth.set_exports(agent, [alpha])
    assert [row["name"] for row in answered] == [name]
    assert agentauth.list_exports()[0]["bound_to"] == name

    moved = {"name": name, "path": f"/srv/moved-{name}"}
    assert agentauth.set_exports(agent, [moved])[0]["state"] == "pending"
    assert agentauth.list_exports()[0]["bound_to"] is None
    assert db.query("SELECT 1 FROM projects WHERE name = $1", [name])


@ON_DATABASE
def test_an_export_binds_to_a_project_only_on_its_path(
    database: dict[str, str],
) -> None:
    """Path equality for an existing project, a synthetic root for a taken path."""
    existing, other = unique(), unique()
    path = f"/srv/{existing}"
    db.query(
        "INSERT INTO projects (name, root_path) VALUES ($1, $2), ($3, $4)",
        [existing, path, other, f"/srv/{other}"],
    )
    exported = unique("test-gamma")
    credential, _ = enrolled(
        [{"name": exported, "path": path}, {"name": "test-delta", "path": path}]
    )
    agent = credential.owner_id
    agentauth.decide_export(agent, exported, "approved", "token:o")
    agentauth.decide_export(agent, "test-delta", "approved", "token:o")
    offered = {row["name"]: row for row in agentauth.list_exports()}
    assert offered[exported]["same_path"] == existing

    with pytest.raises(agentauth.AgentAuthError, match="same path"):
        agentauth.bind_export(agent, exported, "token:o", other)
    bound = agentauth.bind_export(agent, exported, "token:o", existing)
    assert bound["project"] == existing
    with pytest.raises(agentauth.AgentAuthError, match="already bound"):
        agentauth.bind_export(agent, exported, "token:o", existing)
    with pytest.raises(agentauth.AgentAuthError, match="already bound"):
        agentauth.bind_export(agent, "test-delta", "token:o", existing)

    agentauth.bind_export(agent, "test-delta", "token:o", None, "docs")
    rows = db.query("SELECT root_path, type FROM projects WHERE name = 'test-delta'")
    assert rows == [
        {"root_path": storage.agent_root("test-host", path), "type": "docs"}
    ]
    assert agentauth.unbind("test-delta")
    assert not agentauth.unbind("test-delta")
    assert db.query("SELECT 1 FROM projects WHERE name = 'test-delta'")


@ON_DATABASE
def test_a_new_project_takes_a_free_path_and_refuses_a_taken_name(
    database: dict[str, str],
) -> None:
    """The host path is the root when free; a taken name names the way out."""
    name = unique()
    path = f"/srv/{name}"
    credential, _ = enrolled([{"name": name, "path": path}])
    agentauth.decide_export(credential.owner_id, name, "approved", "token:o")
    db.query(
        "INSERT INTO projects (name, root_path) VALUES ($1, $2)",
        [name, f"/srv/other-{name}"],
    )
    with pytest.raises(agentauth.AgentAuthError, match="rename the export"):
        agentauth.bind_export(credential.owner_id, name, "token:o")
    db.query("DELETE FROM projects WHERE name = $1", [name])
    agentauth.bind_export(credential.owner_id, name, "token:o")
    rows = db.query("SELECT root_path FROM projects WHERE name = $1", [name])
    assert rows == [{"root_path": path}]
