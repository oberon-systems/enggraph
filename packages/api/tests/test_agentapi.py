"""The agent routes: what a signature opens, and the socket's life.

The routes are tested against a small fake of the rows `verify_request` reads, so
the signing is the real code. The last test walks the whole enrollment against
the schema when a throwaway database is given.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from enggraph.api import agentapi
from enggraph.core import agentauth, db
from enggraph.core.agentauth import Credential

DATABASE_URL = os.environ.get("EVAL_DATABASE_URL")
AGENT = "11111111-2222-4333-8444-555555555555"
MASTER = {"k1": "alpha-master-key-for-tests-only"}
NOW = 1_900_000_000


class Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        """Start at a fixed moment."""
        self.moment = NOW

    def __call__(self) -> int:
        """Read the clock."""
        return self.moment


class FakeAgent:
    """The one agent the statements of `verify_request` and `seen` ask about."""

    def __init__(self) -> None:
        """Start enabled on generation 1, with no pending one."""
        self.state = "enabled"
        self.generation = 1
        self.pending: int | None = None
        self.seen = 0

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Answer one statement."""
        if sql.startswith("SELECT a.state, s.generation, s.pending_generation"):
            return [
                {
                    "state": self.state,
                    "generation": self.generation,
                    "pending_generation": self.pending,
                }
            ]
        if sql.startswith("SELECT a.state, s.generation"):
            return [{"state": self.state, "generation": self.generation}]
        if sql.startswith("UPDATE agents AS a"):
            self.seen += 1
            return [{"state": self.state, "generation": self.generation}]
        if sql.startswith("UPDATE agents SET state = 'disabled'"):
            self.state = "disabled"
            return []
        if sql.startswith("UPDATE agent_secrets"):
            self.generation, self.pending = params[1], None
            return []
        raise AssertionError(sql)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Stand a hand-moved clock in for the real one."""
    fake = Clock()
    monkeypatch.setattr(agentauth, "now", fake)
    return fake


@pytest.fixture
def keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a master key file and name it."""
    path = tmp_path / "keys"
    path.write_text(f"k1={MASTER['k1']}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MASTER_KEYS_FILE", str(path))
    monkeypatch.setenv("AGENT_CURRENT_KID", "k1")
    return path


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, keys: Path, clock: Clock) -> FakeAgent:
    """Stand the fake rows in for the database."""
    del keys, clock
    fake = FakeAgent()
    monkeypatch.setattr(agentauth.db, "query", fake.query)
    return fake


@pytest.fixture
def client() -> TestClient:
    """Return a client of the router alone: nothing here needs the worker API."""
    app = FastAPI()
    app.include_router(agentapi.router)
    return TestClient(app)


def signed(
    method: str,
    path: str,
    body: bytes = b"",
    generation: int = 1,
    expires_at: int = NOW + 3600,
    at: int = NOW,
    gateway: bool = True,
) -> dict[str, str]:
    """Return the headers an agent sends, signing the path the gateway is asked."""
    credential = Credential(AGENT, "k1", generation, expires_at)
    target = f"{agentauth.GATEWAY_PREFIX}{path}" if gateway else path
    signature = credential.sign(MASTER, method, target, body, at)
    assert signature is not None
    return {
        "X-Credential": credential.header,
        "X-Timestamp": str(at),
        "X-Signature": signature,
    }


def test_register_answers_a_code_and_keeps_a_known_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id is spelled one way, whatever the agent stored."""
    asked: list[tuple[Any, ...]] = []

    def register(*args: Any) -> str:  # noqa: ANN401
        asked.append(args)
        return "code"

    monkeypatch.setattr(agentauth, "register", register)
    answer = client.post(
        "/agents/register",
        json={
            "name": "alpha",
            "labels": ["beta"],
            "agent_id": AGENT.upper(),
            "exports": [{"name": "gamma", "path": "/srv/gamma"}],
        },
    )
    assert answer.status_code == 201 and answer.json() == {"claim_code": "code"}
    assert asked == [
        ("alpha", ["beta"], AGENT, [{"name": "gamma", "path": "/srv/gamma"}])
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": "a b"},
        {"name": "alpha", "agent_id": "nope"},
        {"name": "alpha", "extra": 1},
        {"name": "alpha", "labels": ["a b"]},
    ],
)
def test_register_refuses_what_it_does_not_read(
    client: TestClient, body: dict[str, Any]
) -> None:
    """A name ends up in a root path, and this route takes no credential."""
    assert client.post("/agents/register", json=body).status_code == 422


def test_register_refuses_a_path_that_is_not_canonical(client: TestClient) -> None:
    """The agent canonicalizes its paths, so anything else is not its own."""
    answer = client.post(
        "/agents/register",
        json={"name": "alpha", "exports": [{"name": "beta", "path": "relative"}]},
    )
    assert answer.status_code == 400


def test_claim_waits_refuses_and_signs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """409 while waiting, 401 once refused, and an answer signed under the code."""

    def waiting(_code: str) -> None:
        raise agentauth.WaitingError("waits")

    monkeypatch.setattr(agentauth, "claim", waiting)
    assert client.post("/agents/claim", json={"code": "c"}).status_code == 409

    monkeypatch.setattr(agentauth, "claim", lambda _code: None)
    assert client.post("/agents/claim", json={"code": "c"}).status_code == 401

    credential = Credential(AGENT, "k1", 1, NOW)
    monkeypatch.setattr(agentauth, "claim", lambda _code: (credential, "the-key"))
    granted = client.post("/agents/claim", json={"code": "alpha-code"})
    assert granted.status_code == 200
    assert granted.headers["x-signature"] == agentauth.claim_signature(
        "alpha-code", granted.content
    )
    assert granted.json() == {
        "agent_id": AGENT,
        "credential": {
            "credential": f"k1:1:{NOW}",
            "expires_at": NOW,
            "key": "the-key",
        },
    }


def test_no_master_key_is_a_503_not_a_refusal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agent told 401 would drop a credential that is still good."""
    monkeypatch.setenv("AGENT_MASTER_KEYS_FILE", str(tmp_path / "missing"))

    def no_key(_code: str) -> None:
        raise agentauth.MasterKeyError("no key")

    monkeypatch.setattr(agentauth, "claim", no_key)
    assert client.post("/agents/claim", json={"code": "c"}).status_code == 503
    headers = {"X-Credential": "k1:1:2", "X-Timestamp": "1", "X-Signature": "s"}
    answer = client.post(f"/agents/{AGENT}/credential", headers=headers)
    assert answer.status_code == 503


def test_a_refresh_needs_a_signature_over_the_gateway_path(
    client: TestClient, agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsigned, signed for this service's own path, or stale: all the same 401."""
    del agent
    fresh = Credential(AGENT, "k1", 2, NOW + 3600)
    monkeypatch.setattr(agentauth, "refresh", lambda _id: (fresh, "fresh-key"))
    path = f"/agents/{AGENT}/credential"
    assert client.post(path).status_code == 401
    own = signed("POST", path, gateway=False)
    assert client.post(path, headers=own).status_code == 401
    stale = signed("POST", path, at=NOW - 301)
    assert client.post(path, headers=stale).status_code == 401
    answer = client.post(path, headers=signed("POST", path))
    assert answer.status_code == 200
    assert answer.json() == {
        "credential": fresh.header,
        "expires_at": fresh.expires_at,
        "key": "fresh-key",
    }


def test_exports_are_signed_over_their_exact_bytes(
    client: TestClient, agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another body under the same signature is refused before anything is stored."""
    del agent
    stored: list[Any] = []

    def store(agent_id: str, exports: list[dict[str, str]]) -> list[dict[str, str]]:
        stored.append((agent_id, exports))
        return [{"name": "alpha", "host_path": "/srv/alpha", "state": "pending"}]

    monkeypatch.setattr(agentauth, "set_exports", store)
    path = f"/agents/{AGENT}/exports"
    body = json.dumps({"exports": [{"name": "alpha", "path": "/srv/alpha"}]}).encode()
    headers = {**signed("PUT", path, body), "content-type": "application/json"}
    other = body.replace(b"/srv/alpha", b"/srv/gamma")
    assert client.put(path, content=other, headers=headers).status_code == 401
    assert stored == []
    answer = client.put(path, content=body, headers=headers)
    assert answer.status_code == 200
    assert answer.json()["exports"][0]["state"] == "pending"
    assert stored == [(AGENT, [{"name": "alpha", "path": "/srv/alpha"}])]


def test_a_pending_generation_is_promoted_and_the_old_one_disables(
    client: TestClient, agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of a rotation, through the route."""
    monkeypatch.setattr(agentauth, "set_exports", lambda _id, _e: [])
    path = f"/agents/{AGENT}/exports"
    body = b'{"exports":[]}'
    agent.pending = 2
    new = {**signed("PUT", path, body, 2), "content-type": "application/json"}
    assert client.put(path, content=body, headers=new).status_code == 200
    assert (agent.generation, agent.pending) == (2, None)
    old = {**signed("PUT", path, body, 1), "content-type": "application/json"}
    assert client.put(path, content=body, headers=old).status_code == 401
    assert agent.state == "disabled"
    assert client.put(path, content=body, headers=new).status_code == 401


def test_the_socket_refuses_an_upgrade_without_a_good_signature(
    client: TestClient, agent: FakeAgent, clock: Clock
) -> None:
    """Unsigned, expired, disabled: refused before it is ever accepted."""
    path = f"/agents/{AGENT}/socket"
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(path),
    ):
        pass
    assert refused.value.code == 1008
    expired = signed("GET", path, expires_at=NOW)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(path, headers=expired),
    ):
        pass
    agent.state = "disabled"
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(path, headers=signed("GET", path)),
    ):
        pass
    assert clock.moment == NOW


def test_the_socket_greets_answers_pings_and_notes_the_version(
    client: TestClient, agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hello opens it, and every ping marks the agent as seen."""
    versions: list[str | None] = []
    real = agentauth.seen

    def seen(credential: Credential, version: str | None = None) -> bool:
        versions.append(version)
        return real(credential, version)

    monkeypatch.setattr(agentauth, "seen", seen)
    path = f"/agents/{AGENT}/socket"
    with client.websocket_connect(path, headers=signed("GET", path)) as socket:
        socket.send_json({"type": "hello", "protocol": 1, "version": "1.2.3"})
        assert socket.receive_json() == {"type": "hello", "protocol": 1}
        socket.send_json({"type": "ping"})
        assert socket.receive_json() == {"type": "pong"}
    assert versions == ["1.2.3", None]
    assert agent.seen == 2


def test_the_socket_needs_a_hello_first(client: TestClient, agent: FakeAgent) -> None:
    """Anything else as the first frame closes it."""
    del agent
    path = f"/agents/{AGENT}/socket"
    with client.websocket_connect(path, headers=signed("GET", path)) as socket:
        socket.send_text("not json")
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 1008


def test_the_socket_closes_when_the_credential_lapses(
    client: TestClient, agent: FakeAgent, clock: Clock
) -> None:
    """An expiry passing while connected ends the socket; the agent reconnects."""
    del agent
    path = f"/agents/{AGENT}/socket"
    with client.websocket_connect(path, headers=signed("GET", path)) as socket:
        socket.send_json({"type": "hello", "protocol": 1, "version": "1"})
        socket.receive_json()
        clock.moment = NOW + 3600
        socket.send_json({"type": "ping"})
        assert socket.receive_json() == {"type": "pong"}
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 1000


def test_a_disabled_agent_loses_its_socket_at_the_next_ping(
    client: TestClient, agent: FakeAgent
) -> None:
    """The dashboard writes the state, and the socket reads it."""
    path = f"/agents/{AGENT}/socket"
    with client.websocket_connect(path, headers=signed("GET", path)) as socket:
        socket.send_json({"type": "hello", "protocol": 1, "version": "1"})
        socket.receive_json()
        agent.state = "disabled"
        socket.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 1008


@pytest.fixture
def database(keys: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the pool at the throwaway database, and clear what the test wrote."""
    del keys
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    db.close()
    yield
    for table in ("agents", "agent_secrets", "agent_registrations", "agent_exports"):
        db.query(f"DELETE FROM {table}")  # noqa: S608
    db.query("DELETE FROM project_sources WHERE project LIKE 'test-%'")
    db.query("DELETE FROM projects WHERE name LIKE 'test-%'")
    db.close()


def now_signed(
    credential: dict[str, Any], agent_id: str, method: str, path: str, body: bytes = b""
) -> dict[str, str]:
    """Sign as an agent does: with the header and the key it was handed."""
    parsed = Credential.parse(agent_id, credential["credential"])
    assert parsed is not None
    assert parsed.key(MASTER) == credential["key"]
    at = agentauth.now()
    signature = parsed.sign(MASTER, method, f"/agent{path}", body, at)
    assert signature is not None
    return {
        "X-Credential": credential["credential"],
        "X-Timestamp": str(at),
        "X-Signature": signature,
    }


@pytest.mark.skipif(DATABASE_URL is None, reason="EVAL_DATABASE_URL is not set")
def test_an_agent_enrolls_is_approved_and_one_export_is_bound(
    client: TestClient, database: None
) -> None:
    """The plan's definition of done, against the schema; nothing reads through it."""
    del database
    suffix = uuid.uuid4().hex[:8]
    alpha, beta = f"test-alpha-{suffix}", f"test-beta-{suffix}"
    offered = [
        {"name": alpha, "path": f"/srv/{alpha}"},
        {"name": beta, "path": f"/srv/{beta}"},
    ]
    registered = client.post(
        "/agents/register", json={"name": "test-host", "exports": offered[:1]}
    )
    code = registered.json()["claim_code"]
    assert client.post("/agents/claim", json={"code": code}).status_code == 409

    row = agentauth.list_registrations()[0]
    assert row["fingerprint"] == agentauth.fingerprint(code)
    assert agentauth.approve_registration(row["agent_id"], "token:o")

    claimed = client.post("/agents/claim", json={"code": code})
    assert claimed.status_code == 200
    assert claimed.headers["x-signature"] == agentauth.claim_signature(
        code, claimed.content
    )
    agent_id = claimed.json()["agent_id"]
    held = claimed.json()["credential"]
    assert client.post("/agents/claim", json={"code": code}).status_code == 401

    path = f"/agents/{agent_id}/exports"
    body = json.dumps({"exports": offered}).encode()
    headers = {
        **now_signed(held, agent_id, "PUT", path, body),
        "content-type": "application/json",
    }
    announced = client.put(path, content=body, headers=headers)
    assert announced.status_code == 200
    assert {row["state"] for row in announced.json()["exports"]} == {"pending"}

    assert agentauth.decide_export(agent_id, alpha, "approved", "token:o")
    assert agentauth.decide_export(agent_id, beta, "refused", "token:o")
    bound = agentauth.bind_export(agent_id, alpha, "token:o")
    assert bound["project"] == alpha and bound["verified_at"] is None
    with pytest.raises(agentauth.AgentAuthError):
        agentauth.bind_export(agent_id, beta, "token:o")

    socket_path = f"/agents/{agent_id}/socket"
    headers = now_signed(held, agent_id, "GET", socket_path)
    with client.websocket_connect(socket_path, headers=headers) as socket:
        socket.send_json({"type": "hello", "protocol": 1, "version": "1.2.3"})
        assert socket.receive_json()["type"] == "hello"
    listed = agentauth.list_agents()[0]
    assert listed["version"] == "1.2.3" and listed["connected"] is True
