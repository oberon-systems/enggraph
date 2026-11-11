"""Who may call what: the Host allowlist, and the scope a token holds."""

from __future__ import annotations

from typing import Any

import anyio
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


def test_a_token_host_looks_among_its_exports_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path is the host's own before it is anybody's root; no host asks nothing."""
    asked: list[tuple[str, list[Any]]] = []

    def query(sql: str, params: list[Any]) -> list[dict[str, str]]:
        asked.append((sql, params))
        return [{"project": "beta", "root_path": "/srv/alpha"}]

    monkeypatch.setattr(handlers.db, "query", query)
    assert handlers.project_on_host("/srv/alpha/src", None) == []
    assert asked == []
    found = handlers.project_on_host("/srv/alpha/src", "agent-a")
    assert found[0]["project"] == "beta"
    assert asked[0][1] == ["/srv/alpha/src", "agent-a"]
    assert "project_sources" in asked[0][0]
    assert "ORDER BY length(s.host_path) DESC" in asked[0][0]


def test_describe_project_falls_back_to_the_root_when_the_host_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token's host comes from the principal, and an empty match changes nothing."""
    tables: list[str] = []

    def query(sql: str, params: list[Any]) -> list[dict[str, str]]:
        del params
        tables.append("sources" if "project_sources" in sql else "projects")
        return []

    monkeypatch.setattr(handlers.db, "query", query)
    bound = authn.Principal("mcp", "token:a", "reader", host="agent-a")
    answer = handlers.call_tool("describe_project", {"path": "/srv/alpha"}, None, bound)
    assert "No indexed tree contains /srv/alpha" in answer["content"][0]["text"]
    assert tables == ["sources", "projects"]
    assert handlers.CALLING_HOST.get() is None
    tables.clear()
    handlers.call_tool("describe_project", {"path": "/srv/alpha"}, None)
    assert tables == ["projects"]


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


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the organization lookup and the scope of records named by id."""
    about = {
        "nodes": {"plan-e": "epsilon", "plan-b": "beta", "plan-g": None},
        "prompts": {"prompt-e": "epsilon"},
        "roadmaps": {"road-e": "epsilon", "road-g": None},
    }

    def query(sql: str, params: list[Any]) -> list[dict[str, Any]]:
        if "org_members" in sql:
            return []
        table = next(name for name in about if f"FROM {name}" in sql)
        if params[0] not in about[table]:
            return []
        return [{"about": about[table][params[0]]}]

    monkeypatch.setattr(scope.db, "query", query)


def test_a_project_that_is_no_name_is_not_a_way_past_the_scope(stored: None) -> None:
    """A number, a list or blanks name nothing, and nothing is not everything."""
    token = held("beta")
    for value in (5, ["epsilon"], {"a": 1}, True):
        with pytest.raises(ToolError, match="must be a string"):
            scope.check_access(token, "get_overview", {"project": value}, "epsilon")
        with pytest.raises(ToolError, match="must be a string"):
            scope.read_project({"project": value}, "epsilon")
        with pytest.raises(ToolError, match="must be a string"):
            scope.read_plan_scope({"project": value}, "epsilon")
    with pytest.raises(ToolError, match="across every project"):
        scope.check_access(token, "search_code_nodes", {"project": "  "}, None)
    with pytest.raises(ToolError, match="may not read epsilon"):
        scope.check_access(token, "search_code_nodes", {"project": "  "}, "epsilon")
    scope.check_access(token, "search_code_nodes", {"project": "  "}, "beta")


def test_only_a_tools_own_scope_argument_names_its_scope(stored: None) -> None:
    """A search ignores `about` and a record read ignores `project`."""
    token = held("beta")
    for name, args in (
        ("search_code_nodes", {"query": "x", "about": "beta"}),
        ("search_code", {"query": "x", "node_project": "beta"}),
        ("get_memory", {"project": "beta"}),
    ):
        with pytest.raises(ToolError, match="across every project"):
            scope.check_access(token, name, args, None)
    scope.check_access(token, "get_memory", {"about": "beta"}, None)


def test_a_project_named_by_another_argument_is_weighed(stored: None) -> None:
    """The project to drop and the far end of a link are projects too."""
    token = held("beta", scopes=("read", "write-records"))
    with pytest.raises(ToolError, match="may not read epsilon"):
        scope.check_access(token, "drop_project", {"name": "epsilon"}, "beta")
    scope.check_access(token, "drop_project", {"name": "beta"}, "beta")
    for name in ("save_project_link", "drop_project_link"):
        with pytest.raises(ToolError, match="may not read epsilon"):
            scope.check_access(
                token, name, {"target_project": "epsilon", "relation": "uses"}, "beta"
            )


def test_a_record_named_by_id_is_weighed_by_its_stored_scope(stored: None) -> None:
    """An id is unique across the database, which is why its project is looked up."""
    token = held("beta", scopes=("read", "write-records"))
    for name, args in (
        ("get_plans", {"plan_id": "plan-e"}),
        ("save_plan", {"plan_id": "plan-e", "title": "t", "content": "c"}),
        ("drop_plan", {"plan_id": "plan-e"}),
        ("save_prompt", {"prompt_id": "new", "plan_id": "plan-e"}),
        ("save_prompt", {"prompt_id": "prompt-e", "plan_id": "plan-b"}),
        ("get_prompts", {"prompt_id": "prompt-e"}),
        ("drop_prompt", {"prompt_id": "prompt-e"}),
        ("save_roadmap", {"roadmap_id": "road-e", "title": "t"}),
        ("get_roadmaps", {"roadmap_id": "road-e"}),
        ("drop_roadmap", {"roadmap_id": "road-e"}),
        ("save_roadmap_item", {"roadmap_id": "road-e", "item_id": "i"}),
        ("drop_roadmap_item", {"roadmap_id": "road-e", "item_id": "i"}),
        ("save_issue", {"issue_id": "i", "plan_id": "plan-e"}),
    ):
        with pytest.raises(ToolError, match="may not read epsilon"):
            scope.check_access(token, name, args, "beta")
    scope.check_access(token, "get_plans", {"plan_id": "plan-b"}, "beta")
    scope.check_access(token, "save_plan", {"plan_id": "unknown"}, "beta")


def test_a_global_record_is_read_and_not_changed_by_a_held_token(stored: None) -> None:
    """What is about no project is everybody's to read and nobody's to take."""
    token = held("beta", scopes=("read", "write-records"))
    scope.check_access(token, "get_plans", {"plan_id": "plan-g"}, "beta")
    scope.check_access(token, "get_roadmaps", {"roadmap_id": "road-g"}, "beta")
    scope.check_access(token, "get_memory", {"memory_id": "_global/m"}, "beta")
    scope.check_access(
        token, "save_roadmap_item", {"roadmap_id": "new", "plan_id": "plan-g"}, "beta"
    )
    for name, args in (
        ("save_plan", {"plan_id": "plan-g"}),
        ("drop_plan", {"plan_id": "plan-g"}),
        ("save_prompt", {"prompt_id": "new", "plan_id": "plan-g"}),
        ("drop_roadmap", {"roadmap_id": "road-g"}),
        ("drop_memory", {"memory_id": "_global/m"}),
    ):
        with pytest.raises(ToolError, match="across every project"):
            scope.check_access(token, name, args, "beta")


def test_a_full_record_id_is_weighed_by_its_scope_part(stored: None) -> None:
    """`epsilon/slug` names epsilon whatever `about` and the session say."""
    token = held("beta", scopes=("read", "write-records"))
    for name, key in (
        ("drop_memory", "memory_id"),
        ("drop_suggestion", "suggestion_id"),
        ("drop_issue", "issue_id"),
        ("get_issues", "issue_id"),
    ):
        with pytest.raises(ToolError, match="may not read epsilon"):
            scope.check_access(token, name, {key: "epsilon/slug"}, "beta")
        scope.check_access(token, name, {key: "beta/slug"}, "beta")
        scope.check_access(token, name, {key: "slug"}, "beta")


def test_a_session_does_not_open_on_a_project_the_token_may_not_read(
    monkeypatch: pytest.MonkeyPatch, stored: None
) -> None:
    """Refused before the skills of the project are read."""
    read: list[str | None] = []
    monkeypatch.setattr(server, "read_skills", lambda project: read.append(project))
    client = TestClient(server.app, raise_server_exceptions=False)
    headers = {
        "host": "localhost:3000",
        "x-auth-kind": "mcp",
        "x-auth-subject": "token:a",
        "x-auth-role": "reader",
        "x-auth-scopes": "read",
        "x-auth-projects": "beta",
    }
    for path in ("/mcp/epsilon", "/sse/epsilon"):
        method = "GET" if path.startswith("/sse") else "POST"
        answer = client.request(method, path, headers=headers)
        assert answer.status_code == 403
    assert read == []


def test_a_default_project_the_token_may_not_read_is_not_its_session(
    monkeypatch: pytest.MonkeyPatch, stored: None
) -> None:
    """A bare address falls back to no project rather than to somebody else's."""
    monkeypatch.setattr(server, "DEFAULT_PROJECT", "epsilon")
    request = server.Request(
        {
            "type": "http",
            "headers": [(b"x-auth-kind", b"mcp"), (b"x-auth-projects", b"beta")],
        }
    )
    assert anyio.run(server.route_project, request, ["mcp"]) == (None, None)
    anonymous = server.Request({"type": "http", "headers": []})
    assert anyio.run(server.route_project, anonymous, ["mcp"]) == ("epsilon", None)


def test_the_open_health_check_names_no_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway asks nobody who they are before /health."""
    monkeypatch.setattr(server, "project_names", lambda: ["alpha"])
    answer = anyio.run(server.health)
    assert answer.status_code == 200
    assert b"alpha" not in answer.body
