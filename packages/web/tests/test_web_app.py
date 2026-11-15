"""The app answers what needs no database, and every template compiles."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from enggraph.core import authn
from enggraph.web import mcpclient, pages
from enggraph.web.app import app

HOST = {"host": "web:3002"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Return a client of the app that reads failures as answers."""
    return TestClient(app, raise_server_exceptions=False)


def test_every_template_compiles() -> None:
    """Hold that every template compiles."""
    names = pages.templates.list_templates()
    assert "project/links.html" in names
    for name in names:
        pages.templates.get_template(name)


def test_an_unknown_endpoint_is_a_json_404(client: TestClient) -> None:
    """Hold that an unknown endpoint is a json 404."""
    answer = client.get("/api/gamma", headers=HOST)
    assert answer.status_code == 404
    assert answer.json() == {"error": "No such endpoint"}


def test_an_unknown_host_is_refused(client: TestClient) -> None:
    """Hold that an unknown host is refused."""
    answer = client.get("/api/gamma", headers={"host": "gamma.example.com"})
    assert answer.status_code == 403
    assert "GATEWAY_HOSTS" in answer.json()["error"]


def test_a_write_is_json_from_its_own_origin(client: TestClient) -> None:
    """Hold that a write is json from its own origin."""
    plain = client.post("/api/plans", headers=HOST, content="id=x")
    assert plain.status_code == 415
    crossed = client.post(
        "/api/plans",
        headers={**HOST, "origin": "http://gamma.example.com"},
        json={},
    )
    assert crossed.status_code == 403


def test_a_refused_body_names_the_field(client: TestClient) -> None:
    """Hold that a refused body names the field."""
    answer = client.post("/api/ask", headers=HOST, json={})
    assert answer.status_code == 400
    assert answer.json() == {"error": 'Field "tool" is required'}


def test_a_probe_is_not_answered_with_a_page(client: TestClient) -> None:
    """Hold that a probe is not answered with a page."""
    answer = client.get("/.well-known/oauth-protected-resource")
    assert answer.status_code == 404
    assert answer.headers["content-type"].startswith("text/plain")


def test_an_unknown_address_is_the_page_saying_so(client: TestClient) -> None:
    """Hold that an unknown address is the page saying so."""
    answer = client.get("/gamma/delta")
    assert answer.status_code == 404
    assert pages.NO_SUCH_PAGE in answer.text


def test_the_stylesheet_is_served(client: TestClient) -> None:
    """Hold that the stylesheet is served."""
    assert client.get("/static/styles.css").status_code == 200


@pytest.mark.parametrize(
    "path", ["/gamma/delta", "/static/styles.css", "/api/gamma", "/.well-known/x"]
)
def test_every_answer_refuses_foreign_framing(client: TestClient, path: str) -> None:
    """Hold that every answer refuses foreign framing."""
    answer = client.get(path, headers=HOST)
    assert answer.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert answer.headers["x-frame-options"] == "SAMEORIGIN"
    assert answer.headers["x-content-type-options"] == "nosniff"


def test_a_page_may_name_a_status_of_its_own() -> None:
    """Hold that a page may name a status of its own."""
    answer = pages.render("failed.html", status=None, message="said", missing=True)
    assert answer.status_code == 200
    assert b"said" in answer.body


def test_a_route_that_breaks_answers_a_code_and_an_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whatever a library said goes to the log, under the id in the answer."""

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("could not connect to server at 192.0.2.7")

    monkeypatch.setattr("enggraph.web.routes.plans.db.transaction", broken)
    answer = client.get("/api/plans", headers=HOST)
    assert answer.status_code == 503
    assert answer.headers["x-frame-options"] == "SAMEORIGIN"
    body = answer.json()
    assert body["error"] == "the dashboard could not answer"
    assert "192.0.2.7" not in answer.text
    assert body["error_id"] in caplog.text


@pytest.mark.parametrize("offset", ["-1", "1e30", "99999999999999999999"])
def test_an_offset_out_of_range_is_refused(client: TestClient, offset: str) -> None:
    """Past bigint it used to reach the driver and come back as a 503."""
    answer = client.get(f"/api/plans?offset={offset}", headers=HOST)
    assert answer.status_code == 400


def test_a_number_past_the_float_range_is_a_bad_request(client: TestClient) -> None:
    """A field written with four hundred digits is whole, and out of bounds."""
    body = '{"name": "alpha", "days": ' + "9" * 400 + "}"
    headers = {**HOST, "content-type": "application/json"}
    answer = client.post("/api/tokens", headers=headers, content=body)
    assert answer.status_code in (400, 403)
    assert answer.status_code != 503


def test_sessions_for_names_nobody_indexed_are_not_kept_without_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every new project name sent to Ask opened one more session."""

    class Held:
        def __init__(self, project: str, headers: dict[str, str]) -> None:
            self.task = type("Task", (), {"done": lambda self: False})()
            self.release = type("Flag", (), {"set": lambda self: None})()

    monkeypatch.setattr(mcpclient, "_Held", Held)
    monkeypatch.setattr(mcpclient, "_sessions", {})
    for number in range(mcpclient.MAX_SESSIONS * 3):
        mcpclient._session(mcpclient._key(f"alpha-{number}"))
    assert len(mcpclient._sessions) == mcpclient.MAX_SESSIONS
    assert (f"alpha-{mcpclient.MAX_SESSIONS * 3 - 1}", ()) in mcpclient._sessions


def test_a_relayed_call_goes_out_as_whoever_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two principals never share a session, and nobody sends no header."""
    opened: list[tuple[str, dict[str, str]]] = []

    class Held:
        def __init__(self, project: str, headers: dict[str, str]) -> None:
            opened.append((project, headers))
            self.task = type("Task", (), {"done": lambda self: False})()
            self.release = type("Flag", (), {"set": lambda self: None})()

    monkeypatch.setattr(mcpclient, "_Held", Held)
    monkeypatch.setattr(mcpclient, "_sessions", {})
    reader = authn.Principal("session", "user:a", "reader", ("read",), ("alpha",))
    operator = authn.Principal("session", "user:b", "operator")
    for who in (reader, operator, reader, authn.ANONYMOUS):
        token = mcpclient.PRINCIPAL.set(who)
        mcpclient._session(mcpclient._key("alpha"))
        mcpclient.PRINCIPAL.reset(token)
    assert [headers.get("x-auth-subject") for _, headers in opened] == [
        "user:a",
        "user:b",
        None,
    ]
    assert opened[0][1]["x-auth-projects"] == "alpha"
    assert opened[0][1]["x-auth-role"] == "reader"
    assert authn.from_headers(opened[0][1]) == reader


def test_what_the_api_answers_is_passed_on_only_as_its_own_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list is a library's, and the address of the API is the stack's own."""
    import httpx

    from enggraph.web import upstream

    def answered(status: int, text: str) -> None:
        reply = httpx.Response(status, text=text)
        monkeypatch.setattr(upstream._client, "request", lambda *a, **k: reply)

    answered(422, '{"detail": [{"loc": ["body"], "msg": "MARKER", "input": "x"}]}')
    with pytest.raises(upstream.UpstreamError) as listed:
        upstream.call("GET", "/projects")
    assert "MARKER" not in str(listed.value.message)
    answered(409, '{"detail": "project alpha is being indexed"}')
    with pytest.raises(upstream.UpstreamError) as ours:
        upstream.call("GET", "/projects")
    assert ours.value.message == "project alpha is being indexed"
    answered(502, "<html>MARKER</html>")
    with pytest.raises(upstream.UpstreamError) as page:
        upstream.call("GET", "/projects")
    assert "MARKER" not in str(page.value.message)

    def unreachable(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("MARKER connection refused")

    monkeypatch.setattr(upstream._client, "request", unreachable)
    with pytest.raises(upstream.UpstreamError) as gone:
        upstream.call("GET", "/projects")
    assert "worker-api" not in str(gone.value.message)
    assert "MARKER" not in str(gone.value.message)
    assert "error id" in str(gone.value.message)


def test_a_page_offset_and_a_link_walk_have_an_end() -> None:
    """A number past what is read is the caller's mistake, said as one."""
    from starlette.requests import Request

    from enggraph.web.args import HttpError
    from enggraph.web.routes import links

    def asked(query: str) -> Request:
        return Request({"type": "http", "query_string": query.encode(), "headers": []})

    assert pages.offset_of(asked("offset=50")) == 50
    assert pages.offset_of(asked("offset=x")) == 0
    with pytest.raises(HttpError) as past:
        pages.offset_of(asked("offset=1e19"))
    assert past.value.status == 400
    assert links._within(None, "depth", 1, links.MAX_DEPTH) == 1
    assert links._within("300", "max_steps", 60, links.MAX_STEPS) == 300
    for raw, name, most in (
        ("1000000000", "depth", links.MAX_DEPTH),
        ("1000000000", "max_steps", links.MAX_STEPS),
        ("0", "depth", links.MAX_DEPTH),
        ("1.5", "depth", links.MAX_DEPTH),
    ):
        with pytest.raises(HttpError) as refused:
            links._within(raw, name, 1, most)
        assert refused.value.status == 400


def test_a_nul_on_a_page_is_the_callers_mistake() -> None:
    """The driver's refusal of a NUL is a 400 on a page as it is on the API."""
    error = ValueError("A string literal cannot contain NUL (0x00) characters.")
    assert pages.failed(error).status_code == 400
    assert pages.failed(RuntimeError("MARKER")).status_code == 503


def test_the_ask_page_opens_no_session_for_a_reader_or_an_unknown_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalogue and the pool are an operator's, and a name is a project's."""
    listed: list[str] = []

    async def tools(project: str) -> list[dict[str, object]]:
        listed.append(project)
        return []

    monkeypatch.setattr(mcpclient, "list_tools", tools)
    known = [{"name": "alpha", "type": "codebase", "organizations": []}]
    monkeypatch.setattr(pages, "targets", lambda: known)
    monkeypatch.setenv("AUTH_ENFORCE", "on")
    who = {
        "x-auth-kind": "session",
        "x-auth-subject": "local:a",
        "x-auth-projects": "*",
    }
    reader = {**HOST, **who, "x-auth-role": "reader"}
    for number in range(40):
        assert (
            client.get(f"/?project=alpha-{number}", headers=reader).status_code == 200
        )
    assert client.get("/?project=alpha", headers=reader).status_code == 200
    assert listed == []
    operator = {**HOST, **who, "x-auth-role": "operator"}
    client.get("/?project=nobody", headers=operator)
    assert listed == []
    client.get("/?project=alpha", headers=operator)
    assert listed == ["alpha"]
