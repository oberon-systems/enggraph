"""What a project selects out of its tree, and what the walk leaves behind.

The walk reaches no database: the pair it filters by is settled by
`enggraph.selection` first, so a selection read off disk and one stored in a
row are walked the same way.
"""

from __future__ import annotations

from pathlib import Path

from enggraph.config import IGNORE_FILE, KEEP_FILE
from enggraph.discovery import SpecPair, iter_project_files, load_spec, selects, to_spec


def build(root: Path) -> None:
    """Lay out a mount holding a tree with a vendored directory in it."""
    (root / "configs").mkdir(parents=True)
    (root / "configs" / "prod.yaml").write_text("a: 1\n")
    (root / "configs" / "vendor").mkdir()
    (root / "configs" / "vendor" / "third.yaml").write_text("b: 2\n")
    (root / ".enggraph-ignore").write_text("vendor/\n")
    (root / "agents" / "src").mkdir(parents=True)
    (root / "agents" / "src" / "run.py").write_text("x = 1\n")
    (root / "agents" / "notes.txt").write_text("nothing to parse\n")


def specs(base: Path) -> SpecPair:
    """Load the pair off disk, as `enggraph.selection` does."""
    return load_spec(str(base), KEEP_FILE), load_spec(str(base), IGNORE_FILE)


def selected(root: Path) -> list[str]:
    """Return the project relative paths, sorted for comparison."""
    return sorted(rel for _, rel in iter_project_files(str(root), specs(root)))


def test_every_path_is_relative_to_the_mount(tmp_path: Path) -> None:
    """A node id is the path the tree holds the file at, and nothing more."""
    build(tmp_path)
    assert selected(tmp_path) == [
        "agents/src/run.py",
        "configs/prod.yaml",
    ]


def test_the_ignore_document_prunes_a_whole_directory(tmp_path: Path) -> None:
    """A vendored tree is skipped rather than walked and thrown away."""
    build(tmp_path)
    assert "configs/vendor/third.yaml" not in selected(tmp_path)


def test_a_tree_with_nothing_worth_indexing_selects_nothing(tmp_path: Path) -> None:
    """An empty selection is empty rather than the whole mount."""
    (tmp_path / "notes.txt").write_text("nothing to parse\n")
    assert selected(tmp_path) == []


def test_a_watched_path_under_a_skipped_directory_selects_nothing() -> None:
    """Every commit writes under .git, and none of it is worth a re-index."""
    assert not selects(".git/refs/heads/main.py", None, None)
    assert not selects("node_modules/left-pad/index.js", None, None)


def test_a_watched_path_obeys_the_two_specs() -> None:
    """The rule a walk applies, for a path that arrives without one."""
    keep = to_spec(["*.py"])
    ignore = to_spec(["build/"])
    assert selects("src/run.py", keep, ignore)
    assert not selects("src/run.txt", keep, ignore)
    assert not selects("build/run.py", keep, ignore)


def test_a_watched_path_without_a_keep_list_uses_the_built_in_set() -> None:
    """No keep list is the built-in extension set, not everything."""
    assert selects("src/run.py", None, None)
    assert not selects("notes.txt", None, None)
