"""Tokens and sessions: what is stored, what is compared, what lapses."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from enggraph.core import authn, db

DATABASE_URL = os.environ.get("EVAL_DATABASE_URL")


class FakeDb:
    """Answer the statements authn runs from rows the test hands it."""

    def __init__(self) -> None:
        """Start with no token and no statement run."""
        self.statements: list[tuple[str, list[Any]]] = []
        self.token: dict[str, Any] | None = None

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Record a statement and answer the token lookup."""
        self.statements.append((sql, list(params)))
        if sql.startswith("INSERT INTO auth_tokens"):
            self.token = {
                "id": params[0],
                "secret_sha256": params[1],
                "kind": params[2],
                "name": params[3],
                "role": params[4],
                "scopes": params[5],
                "projects": params[6],
                "host": None,
            }
            return [dict(self.token)]
        if "FROM auth_tokens" in sql and "revoked_at IS NULL" in sql:
            return [] if self.token is None else [dict(self.token)]
        return []

    def updates(self) -> int:
        """Count the last-used stamps written."""
        return sum(1 for sql, _ in self.statements if sql.startswith("UPDATE"))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    """Stand a fake in for the database, with no stamp owed yet."""
    store = FakeDb()
    monkeypatch.setattr(authn.db, "query", store.query)
    monkeypatch.setattr(authn, "_touched", {})
    return store


def test_only_the_hash_of_the_secret_is_stored(fake: FakeDb) -> None:
    """The text is handed back once and the row never holds the secret."""
    text, _ = authn.issue_token("mcp", "alpha")
    token_id, secret = authn.parse_token(text) or ("", "")
    assert re.fullmatch(r"egt_[0-9a-f]{16}_[A-Za-z0-9_-]{43}", text)
    stored = fake.statements[0][1]
    assert secret not in stored and text not in stored
    assert stored[0] == token_id and stored[1] == authn.digest(secret)


def test_a_token_is_compared_by_its_hash(fake: FakeDb) -> None:
    """The right secret opens, a wrong one and a malformed text do not."""
    text, _ = authn.issue_token("api", "beta", projects=["alpha"])
    principal = authn.verify_token(text)
    assert principal is not None
    assert principal.kind == "api" and principal.projects == ("alpha",)
    assert not principal.operator and principal.reads("alpha")
    assert not principal.reads("beta")
    assert authn.verify_token(text[:-1] + ("A" if text[-1] != "A" else "B")) is None
    assert authn.verify_token("egt_") is None
    assert authn.verify_token("Bearer nothing") is None


def test_a_revoked_or_expired_token_is_not_looked_up(fake: FakeDb) -> None:
    """The lookup itself leaves out a revoked and an expired row."""
    text, _ = authn.issue_token("mcp", "alpha")
    authn.verify_token(text)
    lookup = next(sql for sql, _ in fake.statements if sql.startswith("SELECT"))
    assert "revoked_at IS NULL" in lookup
    assert "expires_at IS NULL OR expires_at > now()" in lookup
    fake.token = None
    assert authn.verify_token(text) is None


def test_last_used_is_written_at_most_once_a_minute(
    fake: FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request reads the token; only the first of a minute writes."""
    clock = [1000.0]
    monkeypatch.setattr(authn.time, "monotonic", lambda: clock[0])
    text, _ = authn.issue_token("mcp", "alpha")
    for _ in range(5):
        authn.verify_token(text)
    assert fake.updates() == 1
    clock[0] += authn.TOUCH_SECONDS + 1
    authn.verify_token(text)
    assert fake.updates() == 2


def test_a_request_names_known_kinds_roles_and_scopes(fake: FakeDb) -> None:
    """Nothing outside the vocabularies is stored."""
    with pytest.raises(authn.AuthError):
        authn.issue_token("root", "alpha")
    with pytest.raises(authn.AuthError):
        authn.issue_token("mcp", "alpha", role="owner")
    with pytest.raises(authn.AuthError):
        authn.issue_token("mcp", "alpha", scopes=["everything"])
    with pytest.raises(authn.AuthError):
        authn.issue_token("mcp", "alpha", projects=[])
    with pytest.raises(authn.AuthError):
        authn.issue_token("mcp", "alpha", expires_in_days=0)
    assert fake.statements == []


def test_an_operator_holds_every_scope() -> None:
    """A reader holds what it was given, an operator everything."""
    reader = authn.Principal("mcp", "token:a", "reader", scopes=("write-records",))
    assert reader.may("write-records") and not reader.may("admin")
    assert authn.Principal("session", "local:a", "operator").may("admin")


def test_the_enforce_mode_is_one_of_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset is off; a typo is refused rather than read as off."""
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    assert authn.enforce_mode() == "off"
    monkeypatch.setenv("AUTH_ENFORCE", "WARN")
    assert authn.enforce_mode() == "warn"
    monkeypatch.setenv("AUTH_ENFORCE", "yes")
    with pytest.raises(authn.AuthError):
        authn.enforce_mode()


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the pool at the throwaway database, and clear what a test wrote."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setattr(authn, "_touched", {})
    db.close()
    yield
    db.query(
        "DELETE FROM auth_sessions WHERE subject LIKE 'test-%' OR issuer = 'local'"
    )
    db.query("DELETE FROM auth_principals WHERE subject LIKE 'test-%'")
    db.query("DELETE FROM auth_principals WHERE issuer = 'local'")
    db.query("DELETE FROM auth_tokens WHERE name LIKE 'test-%'")
    db.close()


@pytest.mark.db
@pytest.mark.skipif(DATABASE_URL is None, reason="EVAL_DATABASE_URL is not set")
def test_a_token_lapses_when_revoked_or_expired(database: None) -> None:
    """Against the schema: revoke ends a token and the sessions it opened."""
    text, row = authn.issue_token("operator", "test-alpha", role="operator")
    assert authn.verify_token(text) is not None
    cookie = authn.login_with_token(text)
    assert cookie is not None and authn.read_session(cookie) is not None
    assert authn.revoke_token(row["id"])
    assert authn.verify_token(text) is None
    assert authn.read_session(cookie) is None
    assert not authn.revoke_token(row["id"])
    spent, spent_row = authn.issue_token("mcp", "test-beta", expires_in_days=1)
    db.query(
        "UPDATE auth_tokens SET expires_at = now() - interval '1 second' WHERE id = $1",
        [spent_row["id"]],
    )
    assert authn.verify_token(spent) is None


@pytest.mark.db
@pytest.mark.skipif(DATABASE_URL is None, reason="EVAL_DATABASE_URL is not set")
def test_a_session_slides_and_ends(database: None) -> None:
    """Against the schema: a reader logs in, idles out, and logs out."""
    assert authn.record_login("https://idp.example.com", "test-a", "A", False) == (
        "reader"
    )
    cookie = authn.open_session("https://idp.example.com", "test-a")
    principal = authn.read_session(cookie)
    assert principal is not None and principal.role == "reader"
    assert authn.record_login("https://idp.example.com", "test-a", None, True) == (
        "operator"
    )
    db.query(
        "UPDATE auth_sessions SET last_seen_at = now() - interval '13 hours' "
        "WHERE id_sha256 = $1",
        [authn.digest(cookie)],
    )
    assert authn.read_session(cookie) is None
    other = authn.open_session("https://idp.example.com", "test-a")
    authn.end_session(other)
    assert authn.read_session(other) is None
    stored = db.query("SELECT id_sha256 FROM auth_sessions WHERE subject = 'test-a'")
    assert all(row["id_sha256"] != other for row in stored)
