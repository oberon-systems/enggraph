"""The Agents API and page, and the host a token is bound to: operators alone."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from enggraph.core import agentauth, authn
from enggraph.web import auth
from enggraph.web.app import app

HOST = {"host": "web:3002"}
OPERATOR = {
    **HOST,
    "x-auth-kind": "session",
    "x-auth-subject": "local:token:o",
    "x-auth-role": "operator",
    "x-auth-projects": "*",
}
READER = {**OPERATOR, "x-auth-role": "reader"}
AGENT = "11111111-2222-4333-8444-555555555555"
WAITING = {
    "agent_id": AGENT,
    "name": "alpha",
    "labels": ["beta"],
    "exports": [{"name": "gamma", "path": "/srv/gamma"}],
    "registered_at": None,
    "expires_at": None,
    "approved_at": None,
    "agent_state": "pending",
    "fingerprint": "0a1b2c3d",
}
APPROVED = {
    "id": AGENT,
    "name": "alpha",
    "labels": [],
    "state": "enabled",
    "version": "1.2.3",
    "connected": True,
    "last_seen_at": None,
}
EXPORT = {
    "agent_id": AGENT,
    "name": "gamma",
    "host_path": "/srv/gamma",
    "state": "approved",
    "bound_to": None,
    "same_path": "delta",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a client with one agent waiting and one approved."""
    monkeypatch.setattr(agentauth, "list_registrations", lambda: [WAITING])
    monkeypatch.setattr(agentauth, "list_agents", lambda: [APPROVED])
    monkeypatch.setattr(agentauth, "list_exports", lambda: [EXPORT])
    monkeypatch.setattr(authn, "list_tokens", lambda: [])
    return TestClient(app, raise_server_exceptions=False)


def test_everything_here_is_for_operators(client: TestClient) -> None:
    """Even with enforcement off: an approved agent would outlive switching it on."""
    assert client.get("/api/agents", headers=HOST).status_code == 403
    assert client.get("/api/agents", headers=READER).status_code == 403
    assert client.get("/agents", headers=READER).status_code == 403
    body = {"agent_id": AGENT}
    for path in (
        "/api/agents/approve",
        "/api/agents/refuse",
        "/api/agents/state",
        "/api/agents/revoke",
        "/api/agents/delete",
        "/api/agents/exports/decide",
        "/api/agents/bind",
    ):
        assert client.post(path, headers=READER, json=body).status_code == 403
    unbind = client.request(
        "DELETE", "/api/agents/binding?project=alpha", headers=READER, json={}
    )
    assert unbind.status_code == 403


def test_the_gateway_keeps_a_reader_off_the_agents_api() -> None:
    """The check answers for a reader's token before the route is ever reached."""
    reader = authn.Principal("mcp", "token:r", "reader", scopes=("read",))
    operator = authn.Principal("operator", "token:o", "operator")
    assert auth.refusal(reader, "GET", "/api/agents") is not None
    assert auth.refusal(operator, "POST", "/api/agents/approve") is None


def test_a_token_host_travels_to_the_upstream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP server reads the host from what the gateway hands it."""
    bound = authn.Principal("mcp", "token:h", "reader", host=AGENT)
    monkeypatch.setattr(authn, "verify_token", lambda _text: bound)
    answer = client.get(
        "/auth/check",
        headers={
            "x-original-uri": "/mcp/alpha",
            "x-original-method": "POST",
            "authorization": "Bearer egt_h_x",
        },
    )
    assert answer.headers["x-auth-host"] == AGENT
    assert (
        authn.from_headers({"x-auth-kind": "mcp", "x-auth-host": AGENT}).host == AGENT
    )
    assert authn.from_headers({"x-auth-kind": "mcp"}).host is None


def test_the_listing_gives_each_agent_its_exports(client: TestClient) -> None:
    """What the page draws, as JSON."""
    answer = client.get("/api/agents", headers=OPERATOR).json()
    assert answer["registrations"][0]["fingerprint"] == "0a1b2c3d"
    assert answer["agents"][0]["exports"][0]["name"] == "gamma"


def test_the_page_shows_the_fingerprint_and_offers_the_same_path(
    client: TestClient,
) -> None:
    """An operator checks the fingerprint against the agent's log before approving."""
    page = client.get("/agents", headers=OPERATOR)
    assert page.status_code == 200
    assert "0a1b2c3d" in page.text and "/srv/gamma" in page.text
    assert "Bind to delta" in page.text and "Bind as a new project" in page.text
    assert f'"agent_id": "{AGENT}"' in page.text


def test_a_decision_is_recorded_under_the_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Who approved is the subject the gateway named, not what the body says."""
    asked: list[tuple[Any, ...]] = []

    def record(*args: Any) -> bool:  # noqa: ANN401
        asked.append(args)
        return True

    monkeypatch.setattr(agentauth, "approve_registration", record)
    monkeypatch.setattr(agentauth, "decide_export", record)
    body = {"agent_id": AGENT, "approved_by": "someone else"}
    assert (
        client.post("/api/agents/approve", headers=OPERATOR, json=body).status_code
        == 200
    )
    body = {"agent_id": AGENT, "export": "gamma", "decision": "approved"}
    answer = client.post("/api/agents/exports/decide", headers=OPERATOR, json=body)
    assert answer.status_code == 200
    assert asked == [
        (AGENT, "local:token:o"),
        (AGENT, "gamma", "approved", "local:token:o"),
    ]


def test_nothing_to_decide_is_a_404_and_a_bad_word_a_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second click on a page gone stale says so."""
    monkeypatch.setattr(agentauth, "approve_registration", lambda *_args: False)
    monkeypatch.setattr(agentauth, "set_state", lambda *_args: False)
    body = {"agent_id": AGENT}
    assert (
        client.post("/api/agents/approve", headers=OPERATOR, json=body).status_code
        == 404
    )
    bad = {"agent_id": AGENT, "state": "pending"}
    assert (
        client.post("/api/agents/state", headers=OPERATOR, json=bad).status_code == 400
    )
    gone = {"agent_id": AGENT, "state": "disabled"}
    assert (
        client.post("/api/agents/state", headers=OPERATOR, json=gone).status_code == 404
    )


def test_a_binding_rule_broken_is_a_409_that_says_which(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is what the operator reads on the page."""

    def refuse(*_args: Any) -> None:  # noqa: ANN401
        raise agentauth.AgentAuthError("an export binds only on the same path")

    monkeypatch.setattr(agentauth, "bind_export", refuse)
    body = {"agent_id": AGENT, "export": "gamma", "project": "delta"}
    answer = client.post("/api/agents/bind", headers=OPERATOR, json=body)
    assert answer.status_code == 409 and "same path" in answer.json()["error"]


def test_a_binding_names_the_project_type_and_the_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No project means a new one, of the type the form chose."""
    asked: list[tuple[Any, ...]] = []

    def bind(*args: Any) -> dict[str, str]:  # noqa: ANN401
        asked.append(args)
        return {"project": "gamma"}

    monkeypatch.setattr(agentauth, "bind_export", bind)
    new = {"agent_id": AGENT, "export": "gamma", "type": "docs"}
    assert (
        client.post("/api/agents/bind", headers=OPERATOR, json=new).status_code == 201
    )
    same = {"agent_id": AGENT, "export": "gamma", "project": "delta"}
    assert (
        client.post("/api/agents/bind", headers=OPERATOR, json=same).status_code == 201
    )
    assert asked == [
        (AGENT, "gamma", "local:token:o", None, "docs"),
        (AGENT, "gamma", "local:token:o", "delta", "codebase"),
    ]
    wrong = {"agent_id": AGENT, "export": "gamma", "type": "organization"}
    assert (
        client.post("/api/agents/bind", headers=OPERATOR, json=wrong).status_code == 400
    )


def test_a_token_is_bound_only_to_an_approved_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo here would leave a token matching no host, silently."""
    written: list[tuple[str, str | None]] = []

    def write(token_id: str, host: str | None) -> bool:
        written.append((token_id, host))
        return token_id == "known"

    monkeypatch.setattr(authn, "set_token_host", write)
    monkeypatch.setattr(agentauth, "known_agent", lambda host: host == AGENT)
    path = "/api/token?id=known"
    answer = client.patch(path, headers=OPERATOR, json={"host": "other"})
    assert answer.status_code == 400 and written == []
    assert client.patch(path, headers=OPERATOR, json={"host": AGENT}).status_code == 200
    assert client.patch(path, headers=OPERATOR, json={"host": ""}).status_code == 200
    assert written == [("known", AGENT), ("known", None)]
    gone = client.patch("/api/token?id=gone", headers=OPERATOR, json={"host": ""})
    assert gone.status_code == 404
    assert client.patch(path, headers=READER, json={"host": ""}).status_code == 403


def test_the_tokens_page_shows_and_sets_the_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every token row carries a picker of the approved agents."""
    token = {
        "id": "abc",
        "name": "alpha laptop",
        "kind": "mcp",
        "role": "reader",
        "scopes": ["read"],
        "projects": ["*"],
        "host": AGENT,
        "expires_at": None,
        "last_used_at": None,
        "revoked_at": None,
    }
    monkeypatch.setattr(authn, "list_tokens", lambda: [token])
    page = client.get("/tokens", headers=OPERATOR)
    assert page.status_code == 200
    assert 'data-api="PATCH /api/token?id=abc"' in page.text
    assert f'<option value="{AGENT}" selected>alpha</option>' in page.text
