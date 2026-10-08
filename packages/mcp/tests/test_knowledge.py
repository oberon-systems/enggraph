"""Reading the nodes a record names, and narrowing a read to a node."""

from __future__ import annotations

import pytest

from enggraph.mcp.errors import ToolError
from enggraph.mcp.knowledge import narrow_ids, parse_node_refs


def test_nodes_not_given_keep_the_stored_links() -> None:
    """None is "leave them", an empty list is "remove them"."""
    assert parse_node_refs(None, "alpha") is None


def test_ids_are_read_in_the_record_s_project_and_objects_name_another() -> None:
    """A repeated node is one node."""
    refs = parse_node_refs(
        [
            "src/auth/",
            {"node_id": "src/auth/jwt.ts"},
            {"project": "beta", "node_id": "deploy/"},
            "src/auth/",
        ],
        "alpha",
    )
    assert refs == [
        {"project": "alpha", "node_id": "src/auth/"},
        {"project": "alpha", "node_id": "src/auth/jwt.ts"},
        {"project": "beta", "node_id": "deploy/"},
    ]


def test_a_node_with_no_project_to_fall_back_on_is_refused() -> None:
    """A global record has no project to lend."""
    with pytest.raises(ToolError, match="names no project"):
        parse_node_refs(["src/"], None)


def test_what_is_not_a_node_is_refused() -> None:
    """Each refusal says what the argument should have been."""
    with pytest.raises(ToolError, match="array"):
        parse_node_refs("src/", "alpha")
    with pytest.raises(ToolError, match="node id"):
        parse_node_refs([""], "alpha")
    with pytest.raises(ToolError, match="node id"):
        parse_node_refs([{"project": "beta"}], "alpha")


def test_one_record_names_a_capped_number_of_nodes() -> None:
    """Fifty-one is one too many."""
    many = [f"src/{index}.ts" for index in range(51)]
    with pytest.raises(ToolError, match="at most"):
        parse_node_refs(many, "alpha")


def test_an_id_filter_stays_open_without_a_node_and_intersects_with_one() -> None:
    """None on either side means that side does not narrow."""
    assert narrow_ids(None, None) is None
    assert narrow_ids(["a", "b"], None) == ["a", "b"]
    assert narrow_ids(None, ["b"]) == ["b"]
    assert narrow_ids(["a", "b"], ["b", "c"]) == ["b"]
