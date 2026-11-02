"""The gateway's question, the login form, logout and the Tokens API."""

from __future__ import annotations

import logging

import httpx
import pytest
from starlette.testclient import TestClient

from enggraph.core import authn
from enggraph.web import auth
from enggraph.web.app import app

HOST = {"host": "web:3002"}
READER = authn.Principal("mcp", "token:r", "reader", scopes=("read",))
OPERATOR = authn.Principal("operator", "token:o", "operator")
HELD = authn.Principal("mcp", "token:h", "reader", projects=("alpha",))
TOKENS = {"egt_r_x": READER, "egt_o_x": OPERATOR, "egt_h_x": HELD}
OPERATOR_HEADERS = {
    **HOST,
    "x-auth-kind": "session",
    "x-auth-subject": "local:token:o",
    "x-auth-role": "operator",
    "x-auth-projects": "*",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a client whose tokens and sessions are the ones above."""
    monkeypatch.setattr(authn, "verify_token", TOKENS.get)
    monkeypatch.setattr(
        authn, "read_session", lambda cookie: OPERATOR if cookie == "good" else None
    )
    monkeypatch.setattr(auth, "API_TOKEN", "internal")
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    return TestClient(app, raise_server_exceptions=False)


def check(
    client: TestClient, uri: str, method: str = "GET", **headers: str
) -> httpx.Response:
    """Ask what the gateway asks, for one original request."""
    sent = {"x-original-uri": uri, "x-original-method": method}
    sent.update({key.replace("_", "-"): value for key, value in headers.items()})
    return client.get("/auth/check", headers=sent)


def test_anonymous_passes_while_off_and_warn_and_is_refused_when_on(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The three modes, in the order a rollout goes through them."""
    answer = check(client, "/mcp/alpha", "POST")
    assert answer.status_code == 204
    assert answer.headers["x-auth-kind"] == "anonymous"
    monkeypatch.setenv("AUTH_ENFORCE", "warn")
    with caplog.at_level(logging.WARNING, logger=auth.__name__):
        assert check(client, "/projects?x=1").status_code == 204
    assert "anonymous request: GET /projects" in caplog.text
    monkeypatch.setenv("AUTH_ENFORCE", "on")
    assert check(client, "/projects").status_code == 401
    assert check(client, "/mcp", authorization="Bearer egt_bad").status_code == 401


def test_a_token_is_named_to_the_upstream(client: TestClient) -> None:
    """A good token answers 204 with who it is, for the MCP server to weigh."""
    answer = check(client, "/mcp/alpha", "POST", authorization="Bearer egt_h_x")
    assert answer.status_code == 204
    assert answer.headers["x-auth-kind"] == "mcp"
    assert answer.headers["x-auth-projects"] == "alpha"
    assert answer.headers["x-auth-authorization"] == ""


def test_a_reader_may_not_write_to_the_dashboard(client: TestClient) -> None:
    """Reading is fine, writing is a 403, and Ask is for operators."""
    bearer = {"authorization": "Bearer egt_r_x"}
    assert check(client, "/api/plans", **bearer).status_code == 204
    assert check(client, "/api/plans", "POST", **bearer).status_code == 403
    assert check(client, "/api/ask", "POST", **bearer).status_code == 403
    operator = {"authorization": "Bearer egt_o_x"}
    assert check(client, "/api/plans", "POST", **operator).status_code == 204


def test_a_token_held_to_some_projects_stays_off_the_dashboard(
    client: TestClient,
) -> None:
    """The dashboard reads every project, so it needs a token for all of them."""
    bearer = {"authorization": "Bearer egt_h_x"}
    assert check(client, "/projects", **bearer).status_code == 403
    assert check(client, "/sse/alpha", **bearer).status_code == 204


def test_the_worker_gets_the_internal_token_after_a_check(client: TestClient) -> None:
    """Anonymous traffic keeps its own header, as worker-api checked it before."""
    passed = check(client, "/worker/summaries", "POST", authorization="Bearer egt_o_x")
    assert passed.headers["x-auth-authorization"] == "Bearer internal"
    legacy = check(client, "/worker/summaries", authorization="Bearer old")
    assert legacy.headers["x-auth-authorization"] == "Bearer old"
    reader = check(client, "/worker/claim", "POST", authorization="Bearer egt_r_x")
    assert reader.status_code == 403


def test_a_session_cookie_names_its_person(client: TestClient) -> None:
    """The cookie the login set is what a browser carries."""
    client.cookies.set(auth.SESSION_COOKIE, "good")
    answer = check(client, "/projects", "POST")
    assert answer.status_code == 204
    assert answer.headers["x-auth-role"] == "operator"
    client.cookies.clear()


def test_local_login_opens_a_session_for_an_operator_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode: the form takes an operator token, and nothing else."""
    monkeypatch.setattr(auth, "ISSUER", "")
    monkeypatch.setattr(
        authn, "login_with_token", lambda text: "cookie" if text == "egt_o_x" else None
    )
    page = client.get("/auth/login?next=/projects", headers=HOST)
    assert page.status_code == 200 and 'name="token"' in page.text
    refused = client.post(
        "/auth/login",
        headers={**HOST, "content-type": "application/x-www-form-urlencoded"},
        content="token=egt_r_x&next=/projects",
        follow_redirects=False,
    )
    assert refused.status_code == 401
    opened = client.post(
        "/auth/login",
        headers={**HOST, "content-type": "application/x-www-form-urlencoded"},
        content="token=egt_o_x&next=//example.com/",
        follow_redirects=False,
    )
    assert opened.status_code == 303 and opened.headers["location"] == "/"
    cookie = opened.headers["set-cookie"]
    assert "eg_session=cookie" in cookie and "HttpOnly" in cookie
    assert "SameSite=lax" in cookie and "Secure" not in cookie
    client.cookies.clear()


def test_logout_ends_the_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored session is forgotten, and the cookie with it."""
    ended: list[str] = []
    monkeypatch.setattr(authn, "end_session", ended.append)
    client.cookies.set(auth.SESSION_COOKIE, "good")
    answer = client.post("/auth/logout", headers=HOST, follow_redirects=False)
    assert answer.status_code == 303 and ended == ["good"]
    assert 'eg_session=""' in answer.headers["set-cookie"]
    client.cookies.clear()


def test_tokens_are_for_operators_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with enforcement off: a token minted then would outlive it."""
    monkeypatch.setattr(authn, "list_tokens", lambda: [])
    monkeypatch.setattr(
        authn,
        "issue_token",
        lambda *args: ("egt_new_secret", {"id": "new", "name": args[1]}),
    )
    assert client.get("/api/tokens", headers=HOST).status_code == 403
    assert client.get("/api/tokens", headers=OPERATOR_HEADERS).json() == []
    issued = client.post(
        "/api/tokens", headers=OPERATOR_HEADERS, json={"name": "alpha laptop"}
    )
    assert issued.status_code == 201 and issued.json()["said"] == "egt_new_secret"
    reader = {**OPERATOR_HEADERS, "x-auth-role": "reader"}
    assert client.post("/api/tokens", headers=reader, json={}).status_code == 403
    assert client.get("/tokens", headers=HOST).status_code == 403


def test_the_form_ticks_scopes_and_an_operator_kind_is_an_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkbox per scope, read always; an operator token is never a reader."""
    issued: list[tuple] = []
    monkeypatch.setattr(
        authn,
        "issue_token",
        lambda *args: issued.append(args) or ("egt_new_secret", {"id": "new"}),
    )
    form = {"name": "alpha", "kind": "mcp", "role": "reader"}
    ticked = {**form, "scope_write-records": True, "scope_admin": False}
    client.post("/api/tokens", headers=OPERATOR_HEADERS, json=ticked)
    assert issued[-1][2:4] == ("reader", ["read", "write-records"])
    person = {**form, "kind": "operator"}
    client.post("/api/tokens", headers=OPERATOR_HEADERS, json=person)
    assert issued[-1][2] == "operator"
