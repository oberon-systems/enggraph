"""Who a request comes from: the tokens agents carry and the sessions of people.

A token reads `egt_<id>_<secret>`. Only the sha256 of the secret is stored,
and the id finds the row so the comparison is one constant-time check. A
session is a random cookie, stored the same way.

Run inside a container of the stack, the module is also the command that
issues the first operator token:

    python -m enggraph.core.authn issue --kind operator --name alpha
    python -m enggraph.core.authn list
    python -m enggraph.core.authn revoke <id>
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from enggraph.core import db

TOKEN_PREFIX = "egt_"
KINDS = ("operator", "mcp", "api")
ROLES = ("operator", "reader")
SCOPES = ("read", "write-records", "admin")
ALL_PROJECTS = "*"
ENFORCE_MODES = ("off", "warn", "on")
LOCAL_ISSUER = "local"
SECRET_BYTES = 32
SESSION_IDLE_SECONDS = 12 * 3600
SESSION_LIFE_SECONDS = 7 * 24 * 3600
TOUCH_SECONDS = 60

_touched: dict[str, float] = {}
_touch_lock = threading.Lock()


class AuthError(Exception):
    """A request to issue, revoke or open something that cannot be done."""


@dataclass(frozen=True)
class Principal:
    """Who a request comes from, and what they may do."""

    kind: str
    subject: str
    role: str
    scopes: tuple[str, ...] = ()
    projects: tuple[str, ...] = (ALL_PROJECTS,)
    host: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def operator(self) -> bool:
        """Say whether this principal holds the operator role."""
        return self.role == "operator"

    def may(self, scope: str) -> bool:
        """Say whether this principal holds a scope; an operator holds them all."""
        return self.operator or scope in self.scopes or "admin" in self.scopes

    def reads(self, project: str) -> bool:
        """Say whether this principal may read a project by its name alone."""
        return ALL_PROJECTS in self.projects or project in self.projects


ANONYMOUS = Principal(kind="anonymous", subject="", role="", projects=())


def from_headers(headers: Mapping[str, str]) -> Principal:
    """Return who the gateway said a request is from, by its X-Auth headers.

    Only the gateway sets them: it strips any a client sent before asking.
    """
    kind = headers.get("x-auth-kind", "")
    if kind in ("", "anonymous"):
        return ANONYMOUS

    def listed(name: str) -> tuple[str, ...]:
        return tuple(part for part in headers.get(name, "").split(",") if part)

    return Principal(
        kind=kind,
        subject=headers.get("x-auth-subject", ""),
        role=headers.get("x-auth-role", ""),
        scopes=listed("x-auth-scopes"),
        projects=listed("x-auth-projects"),
        host=headers.get("x-auth-host") or None,
    )


def enforce_mode() -> str:
    """Return AUTH_ENFORCE: off passes everything, warn logs, on refuses."""
    mode = os.environ.get("AUTH_ENFORCE", "off").strip().lower() or "off"
    if mode not in ENFORCE_MODES:
        raise AuthError(f"AUTH_ENFORCE must be one of {', '.join(ENFORCE_MODES)}")
    return mode


def digest(secret: str) -> str:
    """Return the sha256 a secret is stored as."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse_token(text: str) -> tuple[str, str] | None:
    """Split a token into its id and its secret, or None when it is not one."""
    if not text.startswith(TOKEN_PREFIX):
        return None
    token_id, _, secret = text[len(TOKEN_PREFIX) :].partition("_")
    if token_id == "" or secret == "" or not token_id.isalnum():
        return None
    return token_id, secret


def _due(key: str) -> bool:
    """Say whether a last-used stamp is owed, at most once a minute per key."""
    now = time.monotonic()
    with _touch_lock:
        if now - _touched.get(key, float("-inf")) < TOUCH_SECONDS:
            return False
        _touched[key] = now
        return True


def _clean(values: Sequence[str], allowed: Sequence[str], what: str) -> list[str]:
    cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    unknown = [value for value in cleaned if value not in allowed]
    if unknown:
        raise AuthError(f"unknown {what}: {', '.join(unknown)}")
    return cleaned


def issue_token(
    kind: str,
    name: str,
    role: str = "reader",
    scopes: Sequence[str] = ("read",),
    projects: Sequence[str] = (ALL_PROJECTS,),
    expires_in_days: int | None = None,
    created_by: str | None = None,
    host: str | None = None,
) -> tuple[str, db.Row]:
    """Store a new token and return its text, shown this once, and its row."""
    if kind not in KINDS:
        raise AuthError(f"kind must be one of {', '.join(KINDS)}")
    if role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}")
    if name.strip() == "":
        raise AuthError("a token needs a name")
    if expires_in_days is not None and expires_in_days <= 0:
        raise AuthError("expiry must be a positive number of days")
    granted = _clean(scopes, SCOPES, "scope")
    named = list(dict.fromkeys(p.strip() for p in projects if p.strip()))
    if not named:
        raise AuthError('a token names at least one project, or "*"')
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    rows = db.query(
        """INSERT INTO auth_tokens
              (id, secret_sha256, kind, name, role, scopes, projects,
               created_by, expires_at, host)
       VALUES ($1, $2, $3, $4, $5, $6::text[], $7::text[], $8,
               CASE WHEN $9::int IS NULL THEN NULL
                    ELSE now() + make_interval(days => $9::int) END, $10)
    RETURNING id, kind, name, role, scopes, projects, host, created_by,
              created_at, expires_at, last_used_at, revoked_at""",
        [
            token_id,
            digest(secret),
            kind,
            name.strip(),
            role,
            granted,
            named,
            created_by,
            expires_in_days,
            host,
        ],
    )
    return f"{TOKEN_PREFIX}{token_id}_{secret}", rows[0]


def verify_token(text: str) -> Principal | None:
    """Return who a token belongs to, or None when it is unknown or spent."""
    parsed = parse_token(text)
    if parsed is None:
        return None
    token_id, secret = parsed
    rows = db.query(
        """SELECT id, secret_sha256, kind, name, role, scopes, projects, host
         FROM auth_tokens
        WHERE id = $1 AND revoked_at IS NULL
          AND (expires_at IS NULL OR expires_at > now())""",
        [token_id],
    )
    if not rows or not hmac.compare_digest(rows[0]["secret_sha256"], digest(secret)):
        return None
    row = rows[0]
    if _due(f"token:{token_id}"):
        db.query(
            "UPDATE auth_tokens SET last_used_at = now() WHERE id = $1", [token_id]
        )
    return Principal(
        kind=row["kind"],
        subject=f"token:{token_id}",
        role=row["role"],
        scopes=tuple(row["scopes"] or ()),
        projects=tuple(row["projects"] or ()),
        host=row["host"],
        extra={"name": row["name"]},
    )


def list_tokens() -> list[db.Row]:
    """Return every token, newest first, without its hash."""
    return db.query(
        """SELECT id, kind, name, role, scopes, projects, host, created_by,
              created_at, expires_at, last_used_at, revoked_at
         FROM auth_tokens
        ORDER BY created_at DESC, id"""
    )


def set_token_host(token_id: str, host: str | None) -> bool:
    """Bind a token to an agent's host, or free it; False when it is unknown."""
    _, touched = db.run(
        "UPDATE auth_tokens SET host = $2 WHERE id = $1", [token_id, host]
    )
    return touched > 0


def revoke_token(token_id: str) -> bool:
    """Revoke a token and end the sessions opened with it; False when unknown."""
    with db.transaction() as tx:
        _, touched = tx.run(
            """UPDATE auth_tokens SET revoked_at = now()
          WHERE id = $1 AND revoked_at IS NULL""",
            [token_id],
        )
        tx.run(
            "DELETE FROM auth_sessions WHERE issuer = $1 AND subject = $2",
            [LOCAL_ISSUER, f"token:{token_id}"],
        )
    return touched > 0


def record_login(issuer: str, subject: str, display: str | None, operator: bool) -> str:
    """Note a login and return the principal's role, which is this login's.

    An operator no longer listed becomes a reader, and the sessions opened
    while it was one end with the role.
    """
    with db.transaction() as client:
        was = client.query(
            """SELECT role FROM auth_principals
            WHERE issuer = $1 AND subject = $2 FOR UPDATE""",
            [issuer, subject],
        )
        rows = client.query(
            """INSERT INTO auth_principals (issuer, subject, display, role,
                                        last_login_at)
           VALUES ($1, $2, $3, $4, now())
           ON CONFLICT (issuer, subject) DO UPDATE
              SET display = coalesce(EXCLUDED.display, auth_principals.display),
                  last_login_at = now(),
                  role = EXCLUDED.role
        RETURNING role""",
            [issuer, subject, display, "operator" if operator else "reader"],
        )
        if was and was[0]["role"] == "operator" and not operator:
            client.query(
                "DELETE FROM auth_sessions WHERE issuer = $1 AND subject = $2",
                [issuer, subject],
            )
    return str(rows[0]["role"])


def open_session(issuer: str, subject: str, life_seconds: int | None = None) -> str:
    """Store a session for a principal and return the cookie that names it."""
    life = SESSION_LIFE_SECONDS if life_seconds is None else life_seconds
    cookie = secrets.token_urlsafe(SECRET_BYTES)
    db.query(
        """INSERT INTO auth_sessions (id_sha256, issuer, subject, expires_at,
                                  last_seen_at)
       VALUES ($1, $2, $3, now() + make_interval(secs => $4::int), now())""",
        [digest(cookie), issuer, subject, max(min(life, SESSION_LIFE_SECONDS), 1)],
    )
    return cookie


def login_with_token(text: str) -> str | None:
    """Open a session for an operator token at the login form, or None."""
    principal = verify_token(text)
    if principal is None or not principal.operator:
        return None
    rows = db.query(
        """SELECT extract(epoch FROM expires_at - now())::int AS left_seconds
         FROM auth_tokens WHERE id = $1""",
        [principal.subject.removeprefix("token:")],
    )
    left = rows[0]["left_seconds"] if rows else None
    record_login(LOCAL_ISSUER, principal.subject, principal.extra.get("name"), True)
    return open_session(LOCAL_ISSUER, principal.subject, left)


def read_session(cookie: str) -> Principal | None:
    """Return who a session cookie belongs to, or None when it has lapsed."""
    if cookie == "":
        return None
    key = digest(cookie)
    rows = db.query(
        """SELECT s.issuer, s.subject, p.role, p.display
         FROM auth_sessions AS s
         JOIN auth_principals AS p
           ON p.issuer = s.issuer AND p.subject = s.subject
        WHERE s.id_sha256 = $1 AND s.expires_at > now()
          AND s.last_seen_at > now() - make_interval(secs => $2::int)""",
        [key, SESSION_IDLE_SECONDS],
    )
    if not rows:
        return None
    row = rows[0]
    if _due(f"session:{key}"):
        db.query(
            "UPDATE auth_sessions SET last_seen_at = now() WHERE id_sha256 = $1", [key]
        )
    return Principal(
        kind="session",
        subject=f"{row['issuer']}:{row['subject']}",
        role=row["role"],
        extra={"display": row["display"]},
    )


def end_session(cookie: str) -> None:
    """Forget a session, so its cookie opens nothing any more."""
    if cookie != "":
        db.query("DELETE FROM auth_sessions WHERE id_sha256 = $1", [digest(cookie)])


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _print_rows(rows: list[db.Row]) -> None:
    for row in rows:
        state = "revoked" if row["revoked_at"] is not None else "active"
        print(
            f"{row['id']}  {row['kind']:<8} {row['role']:<8} {state:<7} "
            f"projects={','.join(row['projects'] or [])} "
            f"scopes={','.join(row['scopes'] or [])} "
            f"expires={row['expires_at'] or 'never'}  {row['name']}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Issue, list or revoke tokens from a shell inside the stack."""
    parser = argparse.ArgumentParser(prog="python -m enggraph.core.authn")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="issue a token and print it once")
    issue.add_argument("--kind", choices=KINDS, default="mcp")
    issue.add_argument("--name", required=True)
    issue.add_argument("--role", choices=ROLES)
    issue.add_argument("--scopes", default="read")
    issue.add_argument("--projects", default=ALL_PROJECTS)
    issue.add_argument("--days", type=int, default=None)
    commands.add_parser("list", help="list every token, without secrets")
    revoke = commands.add_parser("revoke", help="revoke a token by its id")
    revoke.add_argument("id")
    args = parser.parse_args(argv)
    try:
        if args.command == "issue":
            role = args.role or ("operator" if args.kind == "operator" else "reader")
            text, _ = issue_token(
                args.kind,
                args.name,
                role,
                _split(args.scopes),
                _split(args.projects),
                args.days,
                "cli",
            )
            print(text)
        elif args.command == "list":
            _print_rows(list_tokens())
        elif not revoke_token(args.id):
            print(f"no active token {args.id}", file=sys.stderr)
            return 1
    except AuthError as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
