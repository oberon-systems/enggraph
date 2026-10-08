"""Every MCP tool against a live stack, over the transport a client uses.

Skipped unless EVAL_MCP_URL names a server over the eval corpus
(`make eval-up`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from anyio.from_thread import BlockingPortal, start_blocking_portal
from cases import CASES, FILE, LINKED, PROJECT, SCRATCH, Case
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("EVAL_MCP_URL")

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(MCP_URL is None, reason="EVAL_MCP_URL is not set"),
]


class Live:
    """One session, called from tests that are not coroutines."""

    def __init__(self, portal: BlockingPortal, session: ClientSession) -> None:
        """Take the loop the session lives on, and the session."""
        self._portal = portal
        self._session = session
        self.state: dict[str, str] = {}

    def tools(self) -> list[str]:
        """Return the names the server lists."""
        listed = self._portal.call(self._session.list_tools)
        return [tool.name for tool in listed.tools]

    def raw(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        """Call a tool and return whether it failed, and its text."""
        result = self._portal.call(self._session.call_tool, name, args)
        body = "\n".join(getattr(part, "text", "") for part in result.content)
        return bool(result.isError), body

    def call(self, name: str, args: dict[str, Any]) -> str:
        """Call a tool that must answer."""
        failed, body = self.raw(name, args)
        assert not failed, body
        return body


@pytest.fixture(scope="module")
def live() -> Iterator[Live]:
    """Open one session on the eval project for the whole module."""
    with start_blocking_portal() as portal:
        streams = portal.wrap_async_context_manager(
            streamablehttp_client(f"{MCP_URL}/mcp/{PROJECT}")
        )
        with streams as (read, write, _):
            opened = portal.wrap_async_context_manager(ClientSession(read, write))
            with opened as session:
                portal.call(session.initialize)
                yield Live(portal, session)


def first_json(body: str) -> Any:  # noqa: ANN401
    """Parse an answer; the first call of a session carries a skill check after it."""
    return json.JSONDecoder().raw_decode(body)[0]


def test_every_listed_tool_has_a_case(live: Live) -> None:
    """A tool nobody calls here is a tool nobody checked."""
    assert sorted({case.tool for case in CASES}) == sorted(live.tools())


@pytest.mark.parametrize("case", CASES, ids=[case.tool for case in CASES])
def test_a_tool_answers(live: Live, case: Case) -> None:
    """In order: each write is read back and undone."""
    body = live.call(case.tool, case.args(live.state))
    assert body
    if case.keep is not None:
        value = first_json(body)[0].get(case.keep)
        assert isinstance(value, str) and value, f"no {case.keep} in {body}"
        live.state[case.keep] = value


def test_a_string_is_found_in_the_mounted_trees_with_file_and_line(live: Live) -> None:
    """Whatever separates its words in the file."""
    found = first_json(
        live.call(
            "search_text",
            {"pattern": "web_01_example_com", "loose": True, "project": "*"},
        )
    )
    where = {(one["project"], one["path"], one["line"]) for one in found["matches"]}
    assert (PROJECT, "infra/circuits/web/config.yaml", 3) in where


@pytest.mark.parametrize(
    "spelled", ["web-01.example.com", "WEB_01_example_com", "web-01"]
)
def test_a_host_is_found_by_any_spelling_of_it(live: Live, spelled: str) -> None:
    """Who defines it and who uses it."""
    found = first_json(
        live.call("find_linked_name", {"name": spelled, "kind": "host", "project": "*"})
    )
    provided = {(one["project"], one["node_id"]) for one in found["provides"]}
    assert (PROJECT, "infra/circuits/web/config.yaml") in provided
    assert LINKED in {one["project"] for one in found["takes"]}


def test_beta_is_linked_to_everything_alpha_provides_that_it_takes(live: Live) -> None:
    """One edge per kind of name."""
    links = first_json(live.call("get_project_links", {"direction": "incoming"}))
    kinds = sorted(
        edge["kind"]
        for edge in links["edges"]
        if edge["from"] == LINKED and edge["to"] == PROJECT
    )
    assert kinds == ["bucket", "host", "image", "npm", "pypi"]


def test_a_circuit_is_traced_to_the_node_deploying_onto_its_host(live: Live) -> None:
    """Across the two projects."""
    trace = first_json(live.call("trace", {"node_id": "infra/circuits/web/"}))
    taken = {
        (one["to"]["project"], one["to"]["id"])
        for one in trace["steps"]
        if one["way"] == "taken_by"
    }
    assert (LINKED, "deploy/data/nodes/web-01.example.com.yaml") in taken
    assert trace["chains"]


def edges_of(live: Live, args: dict[str, Any]) -> list[str]:
    """Spell the neighbours of a node as `relation id`."""
    rows = first_json(live.call("get_code_graph_neighbors", args))
    return [f"{row['relation_type']} {row['node_id']}" for row in rows]


def test_a_terraform_circuit_leads_to_its_module_and_its_config(live: Live) -> None:
    """Both are files the circuit reads."""
    edges = edges_of(live, {"node_id": "infra/circuits/web/main.tf"})
    assert "uses_module infra/modules/compute/main.tf" in edges
    assert "reads_file infra/circuits/web/config.yaml" in edges


def test_a_workspace_role_leads_to_the_modules_it_runs(live: Live) -> None:
    """In the project that holds the role."""
    edges = edges_of(
        live, {"project": LINKED, "node_id": "deploy/data/roles/portal.yaml"}
    )
    assert "includes_module deploy/modules/nginx/requires.yaml" in edges


def test_a_change_under_the_alpha_worker_reaches_beta(live: Live) -> None:
    """Through the image beta runs."""
    answer = first_json(live.call("impact_analysis", {"symbol": "worker/main.py"}))
    crossing = answer["impact"]["cross_project"]
    assert any((hit.get("link") or {}).get("from") == LINKED for hit in crossing)


def test_a_memory_comes_back_from_the_code_it_names(live: Live) -> None:
    """From the node, from a file below it, and from a change to that file."""
    record = f"{PROJECT}/{SCRATCH}"
    live.call(
        "save_memory",
        {
            "memory_id": SCRATCH,
            "title": "e2e knowledge",
            "text": "e2e",
            "nodes": ["worker/"],
        },
    )
    try:
        read = first_json(live.call("get_memory", {"node_id": FILE}))
        found = {one["id"]: one for one in read}
        assert record in found
        (node,) = found[record]["nodes"]
        assert node["node_id"] == "worker/"
        assert node["missing"] is False

        neighbours = first_json(
            live.call("get_code_graph_neighbors", {"node_id": "worker/"})
        )
        known = {(one["node_id"], one["direction"]) for one in neighbours}
        assert (record, "knowledge") in known

        impact = first_json(live.call("impact_analysis", {"symbol": FILE}))
        assert record in {one["record_id"] for one in impact["knowledge"]}
    finally:
        live.call("drop_memory", {"memory_id": SCRATCH})


def test_a_gap_keeps_the_question_it_failed_on_grouped_by_code(live: Live) -> None:
    """The question is what makes a gap a test case."""
    live.call(
        "save_suggestion",
        {
            "suggestion_id": SCRATCH,
            "title": "e2e gap",
            "detail": "e2e",
            "query": "where is the daily report built",
            "nodes": [FILE],
        },
    )
    try:
        (gap,) = first_json(live.call("get_suggestions", {"suggestion_id": SCRATCH}))
        assert gap["queries"] == ["where is the daily report built"]
        assert [one["node_id"] for one in gap["nodes"]] == [FILE]

        grouped = first_json(live.call("get_suggestions", {"group_by": "directory"}))
        keys = {(one["key"], one["project"]) for one in grouped["groups"]}
        assert ("worker/reports/", PROJECT) in keys
    finally:
        live.call("drop_suggestion", {"suggestion_id": SCRATCH})


def test_a_node_that_does_not_exist_is_refused(live: Live) -> None:
    """And nothing is saved."""
    failed, body = live.raw(
        "save_memory",
        {
            "memory_id": SCRATCH,
            "title": "e2e",
            "text": "e2e",
            "nodes": ["no/such/node.ts"],
        },
    )
    assert failed
    assert "No such node" in body
