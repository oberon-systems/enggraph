"""Where the text of a tool answer is said to come from."""

from __future__ import annotations

import json

import pytest

from enggraph.core import authn
from enggraph.mcp import handlers, provenance, tools


def test_every_tool_is_weighed_as_a_tree_or_as_records() -> None:
    """A tool the table does not name must not pass tree text off as a record."""
    listed = {tool["name"] for tool in tools.list_tools(None, None)}
    assert listed >= provenance.TREE_TOOLS
    written = {name for name in listed if name.split("_")[0] in ("save", "drop")}
    assert not written & provenance.TREE_TOOLS
    assert provenance.origin_of("search_code") == provenance.TREE
    assert provenance.origin_of("get_plans") == provenance.RECORD


def test_the_origin_is_named_once_and_again_where_it_changes() -> None:
    """Stated at the top and inherited below, so an answer does not repeat it."""
    answer = {
        "entries": [{"id": "a.py", "summary": "x", "note": {"why": "seed"}}],
        "knowledge": [{"record": "alpha/plan", "title": "do it"}],
    }
    marked = provenance.stamp(answer, provenance.TREE)
    assert marked["provenance"] == "tree"
    assert "provenance" not in marked["entries"][0]
    assert "provenance" not in marked["entries"][0]["note"]
    assert marked["knowledge"][0]["provenance"] == "record"


def test_a_list_names_the_origin_on_each_of_its_objects() -> None:
    """A list has nowhere to carry a field of its own."""
    marked = provenance.stamp([{"id": "a"}, {"id": "b"}], provenance.RECORD)
    assert [one["provenance"] for one in marked] == ["record", "record"]


@pytest.mark.parametrize(
    ("stored", "told"),
    [("llm", "model"), ("manual", "record"), (None, "unknown"), ("other", "unknown")],
)
def test_a_summary_says_who_wrote_it(stored: str | None, told: str) -> None:
    """A row stored before the source was kept has an origin nobody knows."""
    row = {"id": "a.py", "summary": "does a thing", "summary_source": stored}
    marked = provenance.stamp(row, provenance.TREE)
    assert marked["summary_provenance"] == told
    assert "summary_source" not in marked


def test_a_summary_of_the_same_origin_and_an_empty_one_say_nothing() -> None:
    """The indexer's own summary is the tree's, as everything beside it."""
    row = {"id": "a.py", "summary": "x", "summary_source": "auto"}
    assert "summary_provenance" not in provenance.stamp(row, provenance.TREE)
    empty = {"id": "a.py", "summary": None, "summary_source": None}
    assert "summary_provenance" not in provenance.stamp(empty, provenance.TREE)


def test_an_object_that_names_its_own_origin_keeps_it() -> None:
    """A record listed among the neighbours of a node says so itself."""
    rows = [{"node_id": "a"}, {"node_id": "alpha/plan", "provenance": "record"}]
    marked = provenance.stamp({"neighbours": rows}, provenance.TREE)
    assert "provenance" not in marked["neighbours"][0]
    assert marked["neighbours"][1]["provenance"] == "record"


def test_a_tool_answer_carries_the_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the one door every tool is called by."""
    monkeypatch.setattr(handlers, "check_access", lambda *_: None)
    monkeypatch.setattr(
        handlers,
        "dispatch",
        lambda name, args, project: handlers.json_result({"matches": []}),
    )
    for name, told in (("search_text", "tree"), ("get_plans", "record")):
        answer = handlers.call_tool(name, {}, None, authn.ANONYMOUS)
        assert json.loads(answer["content"][0]["text"])["provenance"] == told
