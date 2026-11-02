"""Who may call what: the Host allowlist, and the scope a token holds."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from enggraph.core import authn
from enggraph.mcp import handlers, scope, server
from enggraph.mcp.errors import ToolError

ORGANIZATIONS = {"gamma": ["alpha", "delta"]}


@pytest.fixture
def members(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the organization lookup without a database."""

    def query(sql: str, params: list[Any]) -> list[dict[str, str]]:
        assert "org_members" in sql
        return [
            {"project": member}
            for name in params[0]
            for member in ORGANIZATIONS.get(name, [])
        ]

    monkeypatch.setattr(scope.db, "query", query)


def held(
    *projects: str, role: str = "reader", scopes: tuple[str, ...] = ("read",)
) -> authn.Principal:
    """Return a token's principal, held to some projects."""
    return authn.Principal("mcp", "token:a", role, scopes=scopes, projects=projects)


def test_an_unknown_host_is_refused() -> None:
    """The DNS rebinding guard answers before any session opens."""
    client = TestClient(server.app, raise_server_exceptions=False)
    answer = client.post("/mcp", headers={"host": "gamma.example.com"}, json={})
    assert answer.status_code == 403
    assert "gamma.example.com" in answer.text
    browser = client.post(
        "/mcp",
        headers={"host": "localhost:3000", "origin": "http://gamma.example.com"},
        json={},
    )
    assert browser.status_code == 403


def test_anonymous_calls_keep_todays_behaviour() -> None:
    """With enforcement off nobody is named, and nothing is checked."""
    scope.check_access(authn.ANONYMOUS, "drop_project", {"project": "*"}, None)


def test_a_token_reads_only_its_projects(members: None) -> None:
    """Named directly, through the session, or as a member of its organization."""
    token = held("beta", "gamma")
    scope.check_access(token, "get_overview", {"project": "beta"}, None)
    scope.check_access(token, "get_overview", {}, "alpha")
    scope.check_access(token, "get_memory", {"about": "gamma"}, "beta")
    with pytest.raises(ToolError, match="may not read epsilon"):
        scope.check_access(token, "get_overview", {"project": "epsilon"}, None)
    with pytest.raises(ToolError, match="may not read epsilon"):
        scope.check_access(token, "get_overview", {}, "epsilon")
    with pytest.raises(ToolError, match="may not read epsilon"):
        scope.check_access(
            token, "get_plans", {"node_id": "./", "node_project": "epsilon"}, "beta"
        )


def test_a_held_token_may_not_read_across_projects(members: None) -> None:
    """Every project at once is outside anything short of all of them."""
    token = held("beta")
    for name, args, session in (
        ("list_projects", {}, "beta"),
        ("search_code_nodes", {"query": "x", "project": "*"}, "beta"),
        ("search_code_nodes", {"query": "x", "project_type": "docs"}, None),
        ("search_code_nodes", {"query": "x"}, None),
        ("describe_project", {"path": "/srv/alpha"}, "beta"),
        ("get_plans", {"project": "*"}, "beta"),
    ):
        with pytest.raises(ToolError, match="across every project"):
            scope.check_access(token, name, args, session)
    scope.check_access(held("*"), "list_projects", {}, None)


def test_a_write_needs_the_operator_role_or_the_scope() -> None:
    """A read-only token may read and may not save, drop or touch a hash."""
    reader = held("*")
    for name in ("save_memory", "drop_project", "set_file_hash"):
        with pytest.raises(ToolError, match="may only read"):
            scope.check_access(reader, name, {"project": "beta"}, None)
    scope.check_access(reader, "get_memory", {}, "beta")
    writer = held("*", scopes=("read", "write-records"))
    scope.check_access(writer, "save_memory", {}, "beta")
    scope.check_access(held("*", role="operator"), "drop_project", {}, "beta")


def test_a_refused_call_is_an_answer() -> None:
    """The session stays up and the agent reads why."""
    result = handlers.call_tool("save_memory", {}, "beta", held("*"))
    assert result["isError"] is True
    assert "may only read" in result["content"][0]["text"]


def test_the_principal_is_read_from_the_gateway_headers() -> None:
    """What /auth/check answered is what the MCP server weighs."""
    principal = authn.from_headers(
        {
            "x-auth-kind": "mcp",
            "x-auth-subject": "token:a",
            "x-auth-role": "reader",
            "x-auth-scopes": "read,write-records",
            "x-auth-projects": "alpha,beta",
        }
    )
    assert principal.projects == ("alpha", "beta")
    assert principal.may("write-records") and not principal.operator
    assert authn.from_headers({"x-auth-kind": "anonymous"}) is authn.ANONYMOUS
    assert authn.from_headers({}) is authn.ANONYMOUS
