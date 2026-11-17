"""The tool list is the contract: every name, description and argument.

The fixture is what the previous server answered, for a session opened on
no project and for one opened on a project.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from enggraph.core import failures, trees
from enggraph.mcp import handlers, tools
from enggraph.mcp.errors import ToolError, message
from enggraph.mcp.search import MODES

PLACEHOLDER = "@@P@@"


@pytest.fixture(scope="module")
def recorded() -> dict[str, Any]:
    """Load what the previous server answered."""
    path = Path(__file__).with_name("fixtures") / "tools.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_session_without_a_project_sees_the_list_as_recorded(
    recorded: dict[str, Any],
) -> None:
    """Every scope argument says the project has to be named."""
    assert tools.list_tools(None, None) == recorded["none"]


def test_a_session_on_a_project_sees_the_list_as_recorded(
    recorded: dict[str, Any],
) -> None:
    """Every scope argument names the default."""
    assert tools.list_tools(PLACEHOLDER, None) == recorded["named"]


def test_an_organization_is_told_what_a_read_there_covers(
    recorded: dict[str, Any],
) -> None:
    """With the count spelled for none, one and several."""
    note = tools.organization_note
    assert note(PLACEHOLDER, 3) == recorded["organization_note"]
    assert note(PLACEHOLDER, 0) == recorded["organization_note_empty"]
    assert note(PLACEHOLDER, 1) == recorded["organization_note_one"]
    record_note = tools.organization_record_note(PLACEHOLDER, 3)
    assert record_note == recorded["organization_record_note"]


def test_the_organization_note_reaches_the_scope_arguments() -> None:
    """A search scope is the one argument it is left off."""
    listed = {tool["name"]: tool for tool in tools.list_tools(PLACEHOLDER, 2)}
    properties = listed["get_overview"]["inputSchema"]["properties"]
    assert "is an organization" in properties["project"]["description"]
    search = listed["search_code"]["inputSchema"]["properties"]
    assert "is an organization" not in search["project"]["description"]


def test_search_code_lists_the_modes_it_takes() -> None:
    """The refusal of any other value names the same three."""
    listed = {tool["name"]: tool for tool in tools.list_tools(None, None)}
    mode = listed["search_code"]["inputSchema"]["properties"]["mode"]
    assert tuple(mode["enum"]) == MODES


def test_every_listed_tool_has_a_handler_and_no_handler_is_unlisted() -> None:
    """A tool an agent is shown and cannot call is worse than a missing one."""
    listed = {tool["name"] for tool in tools.list_tools(None, None)}
    handled = {
        *handlers.EARLY,
        *handlers.SCOPED,
        *handlers.SYMBOL_TOOLS,
        *handlers.LINK_TOOLS,
        *handlers.EXPORT_TOOLS,
        *handlers.SKILL_TOOLS,
    }
    assert listed == handled
    assert len(listed) == 52


def clique(size: int) -> list[dict[str, str]]:
    """Return the edges of a graph in which every node touches every other."""
    names = [f"n{index:03}" for index in range(size)]
    return [
        {"source_id": one, "target_id": other}
        for position, one in enumerate(names)
        for other in names[position + 1 :]
    ]


def touching(edges: list[dict[str, str]], nodes: list[str]) -> list[dict[str, str]]:
    """Return the edges with an end among the nodes, as the walk's query does."""
    inside = set(nodes)
    return [
        edge
        for edge in edges
        if edge["source_id"] in inside or edge["target_id"] in inside
    ]


def test_a_path_walk_expands_no_node_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clique with no way out is answered after one hop, not after every walk."""
    edges = clique(200)
    asked: list[list[str]] = []

    def query(sql: str, params: list[Any]) -> list[dict[str, str]]:
        asked.append(params[1])
        return touching(edges, params[1])[: params[2]]

    monkeypatch.setattr(handlers.db, "query", query)
    assert handlers.walk_between("alpha", "n000", "elsewhere", 10) is None
    assert len(asked) == 2 and len(asked[1]) == 199
    assert handlers.walk_between("alpha", "n000", "n150", 10) == ["n000", "n150"]
    assert handlers.walk_between("alpha", "n000", "n000", 10) == ["n000"]


def test_a_path_is_the_shortest_through_either_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edges are followed both ways, and the hop limit ends the walk."""
    edges = [
        {"source_id": "a", "target_id": "b"},
        {"source_id": "c", "target_id": "b"},
        {"source_id": "c", "target_id": "d"},
    ]

    def query(sql: str, params: list[Any]) -> list[dict[str, str]]:
        return touching(edges, params[1])

    monkeypatch.setattr(handlers.db, "query", query)
    assert handlers.walk_between("alpha", "a", "d", 6) == ["a", "b", "c", "d"]
    assert handlers.walk_between("alpha", "a", "d", 2) is None
    scope = {"project": "alpha", "organization": False, "members": ["alpha"]}
    answer = handlers.shortest_path({"source_id": "a", "target_id": "c"}, scope)
    assert '"depth": 2' in answer["content"][0]["text"]


def test_a_call_carrying_too_much_text_is_refused() -> None:
    """Refused before anything is looked up, nested arguments included."""
    large = "x" * (handlers.MAX_CALL_CHARS + 1)
    for args in ({"text": large}, {"tags": [large]}, {"nodes": [{"node_id": large}]}):
        result = handlers.call_tool("save_memory", args, "alpha")
        assert result["isError"] is True
        assert "more than" in result["content"][0]["text"]


@pytest.mark.parametrize("position", [True, 0, -5])
def test_a_roadmap_position_starts_at_one(position: object) -> None:
    """A boolean is no number, and nothing stands before the first place."""
    args = {"roadmap_id": "r", "item_id": "i", "position": position}
    with pytest.raises(ToolError, match="whole number from 1"):
        handlers.save_roadmap_item(args, "alpha")


def test_a_roadmap_position_ends_with_the_roadmap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the end is refused before any item moves."""
    statements: list[str] = []

    class Client:
        def query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
            statements.append(sql)
            if "count(*)" in sql:
                return [{"items": 3}]
            return [] if "FROM roadmap_items" in sql else [{"?column?": 1}]

    @contextmanager
    def transaction() -> Iterator[Client]:
        yield Client()

    monkeypatch.setattr(handlers.db, "transaction", transaction)
    args = {"roadmap_id": "r", "item_id": "new", "title": "t", "position": 2**40}
    with pytest.raises(ToolError, match="1 to 4"):
        handlers.save_roadmap_item(args, "alpha")
    assert not any("UPDATE" in sql or "INSERT" in sql for sql in statements)


def test_a_driver_error_reaches_an_agent_as_a_code_and_an_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Postgres text names tables and columns; the agent gets none of it."""
    said = message(psycopg2.OperationalError('relation "tokens" does not exist'))
    assert "tokens" not in said
    assert said.startswith("the database refused the request")
    assert said.rsplit("error id ", 1)[1].rstrip(")") in caplog.text
    assert "tokens" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        ToolError("no plan named alpha"),
        failures.Refusal("project 'alpha' is being indexed"),
        trees.TreeUnavailable("the agent of alpha is offline"),
    ],
)
def test_a_refusal_written_here_keeps_its_words(error: Exception) -> None:
    """A sentence an agent can act on is the answer itself."""
    assert message(error) == str(error)


def test_any_other_error_is_a_code_and_an_id() -> None:
    """A KeyError's text is a key, an OSError's a path."""
    said = message(KeyError("/home/alpha/.config"))
    assert "alpha" not in said
    assert said.startswith("the tool failed (error id ")


def neighbours_of(edges: list[tuple[str, str, str]]) -> Any:  # noqa: ANN401
    """Answer the neighbours query as the database does, from (side, relation, id)."""

    def query(sql: str, params: list[Any]) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list[str]] = {}
        for side, relation, node in edges:
            if params[3] is None or relation == params[3]:
                groups.setdefault((side, relation), []).append(node)
        ranked = [
            {
                "project": "alpha",
                "node_id": node,
                "relation_type": relation,
                "direction": side,
                "total": len(nodes),
                "place": place,
                "type": "file",
                "file_path": node,
                "summary": None,
                "summary_source": None,
            }
            for (side, relation), nodes in groups.items()
            for place, node in enumerate(sorted(nodes), 1)
        ]
        ranked.sort(key=lambda row: (row["place"], row["direction"], row["node_id"]))
        picked = [
            {key: value for key, value in row.items() if key != "place"}
            for row in ranked[: params[2]]
        ]
        return sorted(
            picked,
            key=lambda row: (row["direction"], row["relation_type"], row["node_id"]),
        )

    return query


def neighbours(
    monkeypatch: pytest.MonkeyPatch,
    edges: list[tuple[str, str, str]],
    **args: str,
) -> list[dict[str, Any]]:
    """Ask for the neighbours of a.py over these edges alone."""
    monkeypatch.setattr(handlers.db, "query", neighbours_of(edges))
    monkeypatch.setattr(handlers, "node_links", lambda *_: [])
    monkeypatch.setattr(handlers, "knowledge_for", lambda *_: [])
    scope = {"project": "alpha", "organization": False, "members": ["alpha"]}
    answer = handlers.get_code_graph_neighbors({"node_id": "a.py", **args}, scope)
    return json.loads(answer["content"][0]["text"])


def kept(rows: list[dict[str, Any]], side: str, relation: str) -> int:
    """Count the edges of one relation an answer listed."""
    return sum(
        1
        for row in rows
        if row["direction"] == side and row["relation_type"] == relation
    )


def test_a_file_s_imports_are_not_lost_behind_what_it_contains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every relation gets its share, and the one cut short says so."""
    edges = [("incoming", "contains", "./")]
    edges += [("outgoing", "contains", f"a.py::f{n:02}()") for n in range(80)]
    edges += [("outgoing", "imports_from", f"m{n:02}.py") for n in range(20)]
    rows = neighbours(monkeypatch, edges)
    assert kept(rows, "outgoing", "imports_from") == 20
    assert kept(rows, "incoming", "contains") == 1
    assert kept(rows, "outgoing", "contains") == 29
    (cut,) = [row for row in rows if row["direction"] == "truncated"]
    assert (cut["relation_type"], cut["side"], cut["shown"], cut["total"]) == (
        "contains",
        "outgoing",
        29,
        80,
    )
    assert '"contains"' in cut["note"]
    assert all("total" not in row for row in rows if row["direction"] != "truncated")


def test_a_module_many_files_import_still_lists_its_own_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incoming edges no longer fill the answer before any outgoing one."""
    edges = [("incoming", "imports_from", f"user{n:02}.py") for n in range(60)]
    edges += [("outgoing", "imports_from", f"m{n}.py") for n in range(5)]
    rows = neighbours(monkeypatch, edges)
    assert kept(rows, "outgoing", "imports_from") == 5
    assert kept(rows, "incoming", "imports_from") == 45


def test_one_relation_is_listed_alone_on_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cut is named again, since no larger answer is left to ask for."""
    edges = [("outgoing", "contains", f"a.py::f{n:02}()") for n in range(80)]
    edges += [("outgoing", "imports_from", "m.py")]
    rows = neighbours(monkeypatch, edges, relation_type="contains")
    assert {row["relation_type"] for row in rows} == {"contains"}
    assert kept(rows, "outgoing", "contains") == 50
    (cut,) = [row for row in rows if row["direction"] == "truncated"]
    assert (cut["shown"], cut["total"]) == (50, 80)
    assert "at most" in cut["note"]


def test_a_node_that_fits_names_no_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """A complete answer carries edges alone."""
    edges = [("incoming", "contains", "./"), ("outgoing", "imports_from", "m.py")]
    rows = neighbours(monkeypatch, edges)
    assert [row["direction"] for row in rows] == ["incoming", "outgoing"]
