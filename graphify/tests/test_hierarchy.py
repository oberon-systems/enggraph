"""Directory nodes, built from the file ids alone, and their first summaries."""

from __future__ import annotations

from typing import Any

from enggraph import hierarchy
from enggraph.hierarchy import (
    ROOT_ID,
    auto_summary,
    depth_of,
    display_name,
    parent_of,
    plan_tree,
)


def test_parents_climb_to_the_root() -> None:
    """A file's directory, that directory's parent, and then the repository."""
    assert parent_of("src/alpha/queue.py") == "src/alpha/"
    assert parent_of("src/alpha/") == "src/"
    assert parent_of("src/") == ROOT_ID
    assert parent_of("README.md") == ROOT_ID


def test_depth_counts_segments() -> None:
    """The root is the shallowest, so it is described last."""
    assert depth_of(ROOT_ID) == 0
    assert depth_of("src/") == 1
    assert depth_of("src/alpha/") == 2


def test_the_tree_lists_directories_before_files() -> None:
    """Every directory on the way up is created, and each child once."""
    tree = plan_tree(["src/alpha/queue.py", "src/beta.py", "README.md"])
    assert tree[ROOT_ID] == ["src/", "README.md"]
    assert tree["src/"] == ["src/alpha/", "src/beta.py"]
    assert tree["src/alpha/"] == ["src/alpha/queue.py"]


def test_a_readme_names_its_directory() -> None:
    """The landing file's summary is the best line there is without a model."""
    children = ["docs/", "src/beta/README.md", "src/beta/queue.py"]
    summaries = {"src/beta/README.md": "Beta queue service.", "src/beta/queue.py": ""}
    assert auto_summary("src/beta/", children, summaries) == "Beta queue service."


def test_without_a_landing_file_the_listing_says_what_is_there() -> None:
    """Counts first, then the first names, as a file with no prose does."""
    children = ["src/alpha/gamma/", "src/alpha/a.py", "src/alpha/b.py"]
    assert auto_summary("src/alpha/", children, {}) == (
        "Holds 2 files and 1 directory: gamma/, a.py, b.py"
    )


def test_the_root_takes_the_project_description() -> None:
    """The repository node is what a whole-project question lands on."""
    assert auto_summary(ROOT_ID, ["src/"], {}, "Alpha example service.") == (
        "Alpha example service."
    )


def test_display_names_keep_the_slash() -> None:
    """A listing tells a directory from a file by it."""
    assert display_name("src/alpha/") == "alpha/"
    assert display_name("src/alpha/queue.py") == "queue.py"


class Recording:
    """A cursor that answers the file query and keeps every statement."""

    def __init__(self, files: list[tuple[str, str]]) -> None:
        """Take the file rows the first query answers with."""
        self.files = files
        self.sent: list[tuple[str, tuple[Any, ...]]] = []
        self.answer: list[Any] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Keep the statement and pick what the next fetch returns."""
        self.sent.append((" ".join(sql.split()), params))
        self.answer = self.files if "type = 'file'" in sql else []

    def fetchall(self) -> list[Any]:
        """Return the rows of the last statement."""
        return self.answer

    def fetchone(self) -> tuple[str] | None:
        """No project row: no description to fall back on."""
        return None

    def params_of(self, prefix: str) -> tuple[Any, ...]:
        """Return the parameters of the statement starting with `prefix`."""
        return next(params for sql, params in self.sent if sql.startswith(prefix))


def test_rebuild_writes_the_ladder_and_nothing_else() -> None:
    """Old directories go, every level is written, and no tree is read."""
    cursor = Recording([("src/alpha/queue.py", "Claims tasks."), ("README.md", "")])

    assert hierarchy.rebuild(cursor, "alpha") == 3  # type: ignore[arg-type]
    assert any(sql.startswith("DELETE FROM graph_nodes") for sql, _ in cursor.sent)
    nodes = cursor.params_of("INSERT INTO graph_nodes")
    assert set(nodes[1]) == {ROOT_ID, "src/", "src/alpha/"}
    assert nodes[2][nodes[1].index(ROOT_ID)] == "alpha"
    edges = cursor.params_of("INSERT INTO graph_edges")
    assert ("src/alpha/", "src/alpha/queue.py") in zip(edges[1], edges[2], strict=True)
