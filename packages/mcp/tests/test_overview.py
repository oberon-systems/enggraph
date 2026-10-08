"""Which node an overview starts at, and how its rows are nested and cut."""

from __future__ import annotations

from typing import Any

from enggraph.mcp.overview import ROOT_ID, candidate_ids, shape


def row(
    node_id: str, parent: str | None, depth: int, kind: str = "directory"
) -> dict[str, Any]:
    """One row of the walk, as the statement returns it."""
    return {
        "project": "alpha",
        "id": node_id,
        "parent": parent,
        "depth": depth,
        "name": node_id,
        "type": kind,
        "summary": f"Summary of {node_id}",
        "summary_source": "auto",
        "children": 0,
    }


def test_every_spelling_of_the_root_is_the_root() -> None:
    """An agent writes it five ways."""
    for path in ["", ".", "/", "./", "  "]:
        assert candidate_ids(path) == [ROOT_ID]


def test_a_directory_is_tried_with_and_without_its_slash() -> None:
    """The spelling given first."""
    assert candidate_ids("src/api") == ["src/api", "src/api/"]
    assert candidate_ids("./src/api/") == ["src/api/", "src/api"]


def test_children_nest_under_their_parents_directories_first() -> None:
    """A directory outranks a file at the same depth."""
    shaped = shape(
        [
            row("./", None, 0),
            row("README.md", "./", 1, "file"),
            row("src/", "./", 1),
            row("src/a.ts", "src/", 2, "file"),
        ],
        10000,
    )
    assert shaped["truncated"] is False
    assert len(shaped["trees"]) == 1
    root = shaped["trees"][0]["root"]
    assert [item["id"] for item in root["items"]] == ["src/", "README.md"]
    assert [item["id"] for item in root["items"][0]["items"]] == ["src/a.ts"]


def test_the_deepest_levels_are_cut_first_when_the_budget_runs_out() -> None:
    """Breadth first, so the map stays whole near the top."""
    rows = [
        row("./", None, 0),
        *[row(f"d{at}/", "./", 1) for at in range(20)],
        row("d0/deep.ts", "d0/", 2, "file"),
    ]
    shaped = shape(rows, 200)
    assert shaped["truncated"] is True
    assert shaped["used"] <= 200
    items = shaped["trees"][0]["root"]["items"]
    assert len(items) < 20
    assert "items" not in items[0]
