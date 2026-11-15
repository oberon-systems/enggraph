"""The gateway's question, the login form, logout and the Tokens API."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx
import pytest
from authlib.integrations.httpx_client import OAuthError
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey
from starlette.testclient import TestClient

from enggraph.core import authn
from enggraph.web import auth
from enggraph.web.app import app
from enggraph.web.routes import projects

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
    monkeypatch.setenv("AUTH_ENFORCE", "off")
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


def test_a_token_held_to_some_projects_stays_off_the_worker_api(
    client: TestClient,
) -> None:
    """The worker API weighs no project, so a held token never reaches it."""
    bearer = {"authorization": "Bearer egt_h_x"}
    for uri in (
        "/worker/content?project=beta&path=README.md",
        "/worker/projects/beta/settings",
        "/worker/projects",
        "/worker/listing",
    ):
        answer = check(client, uri, **bearer)
        assert answer.status_code == 403
        assert "x-auth-authorization" not in answer.headers
    assert check(client, "/worker/projects", "POST", **bearer).status_code == 403
    everywhere = check(client, "/worker/jobs", authorization="Bearer egt_r_x")
    assert everywhere.status_code == 204


def test_only_an_operator_reaches_the_worker_api_past_its_job_routes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker's token opens the queue; the projects and the probes stay shut."""
    scopes = ("read", "write-records")
    worker = authn.Principal("api", "token:w", "reader", scopes=scopes)
    monkeypatch.setitem(TOKENS, "egt_w_x", worker)
    bearer = {"authorization": "Bearer egt_w_x"}
    for method, uri in (
        ("GET", "/worker/projects"),
        ("POST", "/worker/jobs"),
        ("GET", "/worker/jobs?project=alpha&status=running"),
        ("POST", "/worker/jobs/3/lease"),
        ("POST", "/worker/tasks/7/result"),
    ):
        assert check(client, uri, method, **bearer).status_code == 204
    for method, uri in (
        ("POST", "/worker/projects"),
        ("POST", "/worker/projects/alpha/drop"),
        ("GET", "/worker/content?project=alpha&path=README.md"),
        ("POST", "/worker/summaries/probe"),
        ("GET", "/worker/listing"),
        ("GET", "/worker/jobsx"),
    ):
        assert check(client, uri, method, **bearer).status_code == 403
    operator = {"authorization": "Bearer egt_o_x"}
    dropped = check(client, "/worker/projects/alpha/drop", "POST", **operator)
    assert dropped.status_code == 204


def test_a_probe_is_an_operators_to_ask_for(client: TestClient) -> None:
    """A probe makes the stack call an address, so a reader may not send one."""
    admin = {"authorization": "Bearer egt_r_x"}
    for queue in ("summaries", "embeddings"):
        assert check(client, f"/api/{queue}/probe", "POST", **admin).status_code == 403
    operator = {"authorization": "Bearer egt_o_x"}
    assert check(client, "/api/summaries/probe", "POST", **operator).status_code == 204


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
    assert "/ui/lamps" not in page.text
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


def test_a_session_writes_to_the_worker_api_from_this_site_only(
    client: TestClient,
) -> None:
    """A cookie rides with any same-site page, so its writes name their origin."""
    client.cookies.set(auth.SESSION_COOKIE, "good")
    uri = "/worker/projects/alpha/drop"
    elsewhere = check(client, uri, "POST", host="web:3002", origin="http://web:8080")
    assert elsewhere.status_code == 403
    assert "x-auth-authorization" not in elsewhere.headers
    here = check(client, uri, "POST", host="web:3002", origin="http://web:3002")
    assert here.status_code == 204
    assert check(client, uri, "POST", host="web:3002").status_code == 204
    read = check(client, "/worker/listing", host="web:3002", origin="http://web:8080")
    assert read.status_code == 204
    client.cookies.clear()
    bearer = check(
        client,
        uri,
        "POST",
        host="web:3002",
        origin="http://web:8080",
        authorization="Bearer egt_o_x",
    )
    assert bearer.status_code == 204


def test_an_email_makes_an_operator_only_when_the_issuer_verified_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subject is the issuer's word; an unverified address is the user's."""
    listed = "admin@example.com, Subject-A, sub:beta@example.com"
    subjects, emails = auth.read_operators(listed)
    assert subjects == {"Subject-A", "beta@example.com"}
    assert emails == {"admin@example.com"}
    monkeypatch.setattr(auth, "OPERATOR_SUBJECTS", subjects)
    monkeypatch.setattr(auth, "OPERATOR_EMAILS", emails)
    mail = {"sub": "x", "email": "Admin@example.com"}
    assert auth.listed_operator({**mail, "email_verified": True})
    assert not auth.listed_operator({**mail, "email_verified": False})
    assert not auth.listed_operator({**mail, "email_verified": "true"})
    assert not auth.listed_operator(mail)
    assert auth.listed_operator({"sub": "Subject-A"})
    assert not auth.listed_operator({"sub": "x", "email_verified": True})


def test_a_subject_is_not_an_email_and_is_compared_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subject somebody chose to equal a listed address is not that operator."""
    subjects, emails = auth.read_operators("admin@example.com,Subject-A")
    monkeypatch.setattr(auth, "OPERATOR_SUBJECTS", subjects)
    monkeypatch.setattr(auth, "OPERATOR_EMAILS", emails)
    chosen = {"sub": "admin@example.com", "email": "other@example.com"}
    assert not auth.listed_operator({**chosen, "email_verified": False})
    assert not auth.listed_operator({**chosen, "email_verified": True})
    assert not auth.listed_operator({"sub": "subject-a"})
    assert auth.listed_operator({"sub": "Subject-A"})


def test_an_operator_the_list_dropped_loses_the_role_when_the_dashboard_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list is read at start, so that is when the sessions it opened end."""
    stored = [("Subject-A", "a@example.com"), ("gone", "gone@example.com")]
    asked: list[str] = []

    def demote(issuer: str, listed: object) -> list[str]:
        asked.append(issuer)
        return [name for name, shown in stored if not listed(name, shown)]  # type: ignore[operator]

    monkeypatch.setattr(authn, "demote_unlisted", demote)
    monkeypatch.setattr(auth, "OPERATOR_SUBJECTS", {"Subject-A"})
    monkeypatch.setattr(auth, "OPERATOR_EMAILS", set())
    monkeypatch.setattr(auth, "ISSUER", "")
    auth.reconcile_operators()
    assert asked == []
    monkeypatch.setattr(auth, "ISSUER", "https://id.example.com")
    auth.reconcile_operators()
    assert asked == ["https://id.example.com"]
    assert auth.still_operator("Subject-A", None)
    assert not auth.still_operator("gone", "gone@example.com")
    monkeypatch.setattr(auth, "OPERATOR_EMAILS", {"gone@example.com"})
    assert auth.still_operator("gone", "Gone@example.com")


def test_an_issuer_spelled_with_a_trailing_slash_is_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`iss` is held to what the discovery document says, not to the setting."""
    monkeypatch.setattr(auth, "read_secret", lambda variable: "enggraph")
    key = RSAKey.generate_key(2048)
    issuer = "http://idp.example.com/auth/v1/"
    stated = {
        "iss": issuer,
        "aud": "enggraph",
        "sub": "subject-a",
        "exp": int(time.time()) + 300,
        "nonce": "n",
    }
    token = jwt.encode({"alg": "RS256"}, stated, key)
    keys = {"keys": [key.as_dict(private=False)]}
    assert auth.claims_of(token, keys, "n", issuer)["sub"] == "subject-a"
    with pytest.raises(JoseError):
        auth.claims_of(token, keys, "n", issuer.rstrip("/"))


def test_a_refused_token_exchange_is_the_login_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 from the token endpoint is answered, not redirected into a loop."""

    class Refusing:
        async def __aenter__(self) -> Refusing:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def fetch_token(self, url: str, **kwargs: str) -> dict[str, str]:
            raise OAuthError("invalid_client", "Invalid secret")

    async def document() -> dict[str, str]:
        return {"issuer": "http://idp.example.com/", "token_endpoint": "http://x/t"}

    monkeypatch.setattr(auth, "ISSUER", "http://idp.example.com")
    monkeypatch.setattr(auth, "discovery", document)
    monkeypatch.setattr(auth, "oauth_client", lambda request: Refusing())
    client.cookies.set(auth.LOGIN_COOKIE, "state=s&verifier=v&nonce=n&next=%2F")
    answer = client.get(
        "/auth/callback?state=s&code=c", headers=HOST, follow_redirects=False
    )
    client.cookies.clear()
    assert answer.status_code == 401
    assert "refused" in answer.text


@pytest.mark.parametrize(
    "uri",
    ["/api/%61sk", "/api/a%73k", "/api/ask/tools", "/api/ask/tools?project=alpha"],
)
def test_ask_stays_an_operators_however_it_is_spelled(
    client: TestClient, uri: str
) -> None:
    """The rule reads the path the dashboard routes on, not the bytes sent."""
    reader = {"authorization": "Bearer egt_r_x"}
    assert check(client, uri, "POST", **reader).status_code == 403
    operator = {"authorization": "Bearer egt_o_x"}
    assert check(client, uri, "POST", **operator).status_code == 204


@pytest.mark.parametrize(
    "uri",
    [
        "/api/../worker/projects",
        "/api/%2e%2e/worker/projects",
        "//worker/projects",
        "/worker%2Fprojects",
        "/api/plans%00",
        "/api\\ask",
    ],
)
def test_a_path_the_rules_cannot_weigh_is_refused(client: TestClient, uri: str) -> None:
    """Nginx routes on the merged, decoded path; a rule must not read another."""
    operator = {"authorization": "Bearer egt_o_x"}
    assert check(client, uri, **operator).status_code == 403


def test_only_what_nginx_hands_the_mcp_server_skips_the_dashboard_rules() -> None:
    """A path that merely starts like one is the dashboard's, and weighed as it."""
    for path in ("/messagesx", "/mcpx", "/sse-old", "/message/x", "/mcpx/alpha"):
        assert auth.refusal(HELD, "POST", path) is not None
    for path in ("/mcp", "/mcp/alpha", "/sse", "/sse/alpha", "/message"):
        assert auth.refusal(HELD, "POST", path) is None


@pytest.mark.parametrize(
    "uri",
    ["/mcp#/../api/tokens", "/mcp%23x#/..", "/api/x%0d%0ay", "/api/x%09y", "/a%7f"],
)
def test_a_fragment_mark_or_a_control_character_is_no_path(uri: str) -> None:
    """Nginx reads a literal '#' as part of the path, and a log line ends at CR."""
    assert auth.plain_path(uri) is None


def test_a_header_that_is_no_token_does_not_excuse_a_cookie(
    client: TestClient,
) -> None:
    """The session is what is checked, so the session's origin rule holds."""
    client.cookies.set(auth.SESSION_COOKIE, "good")
    answer = check(
        client,
        "/worker/jobs",
        "POST",
        host="web:3002",
        origin="http://web:8080",
        authorization="Bearer x",
    )
    client.cookies.clear()
    assert answer.status_code == 403


def test_the_global_features_are_an_operators_to_write(client: TestClient) -> None:
    """The admin scope edits a project; where the stack dials is not one."""
    admin = authn.Principal("api", "token:w", "reader", scopes=("read", "admin"))
    uri = "/api/settings/features/summarize"
    assert auth.refusal(admin, "PUT", uri) is not None
    assert auth.refusal(admin, "GET", "/api/settings/features") is None
    assert auth.refusal(OPERATOR, "PUT", uri) is None
    assert auth.refusal(admin, "PUT", "/api/projects/alpha/features/embedding") is None


def test_a_server_address_and_a_probe_are_an_operators_past_the_gateway(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler asks again: a project feature is not a gateway prefix."""
    monkeypatch.setenv("AUTH_ENFORCE", "on")
    monkeypatch.setattr(projects.db, "query", lambda sql, params=None: [{"name": 1}])
    admin = {
        **HOST,
        "x-auth-kind": "api",
        "x-auth-subject": "token:w",
        "x-auth-role": "reader",
        "x-auth-scopes": "read,admin",
        "x-auth-projects": "*",
    }
    url = {"server_url": "http://192.0.2.1:9"}
    for uri in (
        "/api/settings/features/summarize",
        "/api/projects/alpha/features/embedding",
    ):
        assert client.put(uri, headers=admin, json=url).status_code == 403
    for queue in ("summaries", "embeddings"):
        probe = client.post(f"/api/{queue}/probe", headers=admin, json={"url": ""})
        assert probe.status_code == 403


def foreign_host(host: str, listed: str) -> str:
    """Answer what the gateway's map answers: its regexes in order, first match."""
    template = (
        Path(__file__).resolve().parents[3] / "nginx/templates/auth.conf.template"
    )
    block = template.read_text(encoding="utf-8").split("$foreign_host {", 1)[1]
    value = f",{listed},|{host}"
    for line in block.split("}", 1)[0].splitlines():
        found = re.fullmatch(r'\s*"~(\*?)(.*)"\s+([01]);', line)
        if found is None:
            continue
        flags = re.IGNORECASE if found.group(1) else 0
        if re.search(found.group(2), value, flags):
            return found.group(3)
    return "1"


@pytest.mark.parametrize(
    ("host", "listed", "foreign"),
    [
        ("alpha.example.com", "alpha.example.com,beta.example.com:8443", "0"),
        ("BETA.example.com:8443", "alpha.example.com,beta.example.com:8443", "0"),
        ("localhost:3000", "alpha.example.com", "0"),
        ("[::1]:3000", "alpha.example.com", "0"),
        ("gamma.example.com", "*", "0"),
        ("gamma.example.com", "alpha.example.com", "1"),
        ("", "alpha.example.com", "1"),
        ("gamma.example.com,*,", "alpha.example.com", "1"),
        ("gamma.example.com|,gamma.example.com,", "alpha.example.com", "1"),
        ("alpha.example.com|x", "alpha.example.com", "1"),
        ("evil.example.com,alpha.example.com", "alpha.example.com", "1"),
        ("alpha.example.com.evil.example.com", "alpha.example.com", "1"),
        ("xalpha.example.com", "alpha.example.com", "1"),
        ("localhost.evil.example.com", "alpha.example.com", "1"),
    ],
)
def test_a_crafted_host_is_not_taken_for_a_listed_one(
    host: str, listed: str, foreign: str
) -> None:
    """A Host is matched whole against the list, and cannot bring a list along."""
    assert foreign_host(host, listed) == foreign


def test_the_open_locations_clear_what_a_client_calls_itself() -> None:
    """A location with a header of its own inherits none of the server's."""
    conf = Path(__file__).resolve().parents[3] / "nginx/default.conf"
    text = conf.read_text(encoding="utf-8")
    for opening in ("location /auth/v1/ {", "location /agent/agents/ {"):
        block = text.split(opening, 1)[1].split("\n    }", 1)[0]
        for name in ("Kind", "Subject", "Role", "Scopes", "Projects", "Host"):
            assert f'proxy_set_header X-Auth-{name} "";' in block


def test_nobody_is_refused_when_the_mode_is_not_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service started without its compose file is not open by accident."""
    monkeypatch.delenv("AUTH_ENFORCE")
    assert check(client, "/projects").status_code == 401
