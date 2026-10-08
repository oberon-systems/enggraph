"""The app answers what needs no database, and every template compiles."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from enggraph.web import pages
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


def test_a_page_may_name_a_status_of_its_own() -> None:
    """Hold that a page may name a status of its own."""
    answer = pages.render("failed.html", status=None, message="said", missing=True)
    assert answer.status_code == 200
    assert b"said" in answer.body
