"""The tree interface, and that nothing reads a tree around it."""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

from enggraph.core import trees

PACKAGES = pathlib.Path(__file__).resolve().parents[2]
# mounts.py prints the mount list the compose override is generated from.
ALLOWED = {"trees.py", "mounts.py"}
FORBIDDEN_NAMES = {"CODE_ROOT", "SCAN_PATH"}
FORBIDDEN_MODULES = {"watchfiles"}


def test_a_project_is_read_under_its_own_name() -> None:
    """The compose override is generated from the same name."""
    assert trees.of("alpha").where == "/code/alpha"


def test_a_live_directory_is_available(tmp_path: pathlib.Path) -> None:
    """The ordinary case."""
    assert trees.at(str(tmp_path)).available()


def test_a_missing_path_or_a_file_is_not_available(tmp_path: pathlib.Path) -> None:
    """Nothing there, or something that is not a tree."""
    (tmp_path / "beta").write_text("")
    assert not trees.at(str(tmp_path / "alpha")).available()
    assert not trees.at(str(tmp_path / "beta")).available()


def test_a_deleted_directory_behind_a_mount_is_not_available(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind mount keeps a replaced host directory: empty, with no links."""
    fields = list(os.stat(tmp_path))
    fields[3] = 0
    monkeypatch.setattr(trees.os, "stat", lambda path: os.stat_result(fields))
    assert not trees.at(str(tmp_path)).available()


def test_a_walk_is_sorted_and_pruned(tmp_path: pathlib.Path) -> None:
    """Directories and files are filtered by their relative paths."""
    (tmp_path / "b" / "skip").mkdir(parents=True)
    (tmp_path / "b" / "skip" / "x.py").write_text("")
    (tmp_path / "b" / "two.py").write_text("")
    (tmp_path / "a.py").write_text("")
    (tmp_path / "a.txt").write_text("")
    found = trees.at(str(tmp_path)).walk(
        "", lambda rel: rel != "b/skip", lambda rel: rel.endswith(".py")
    )
    assert list(found) == ["a.py", "b/two.py"]


def test_a_linked_file_pointing_out_is_not_contained(tmp_path: pathlib.Path) -> None:
    """A linked file is indexed, and it can point anywhere."""
    root = tmp_path / "alpha"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("not yours")
    (root / "escape.txt").symlink_to(tmp_path / "outside.txt")
    (root / "inside.txt").write_text("yours")
    tree = trees.at(str(root))
    assert tree.contains("inside.txt")
    assert not tree.contains("escape.txt")
    assert not tree.contains("../outside.txt")


def _offences(path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Name | ast.alias):
            name = node.id if isinstance(node, ast.Name) else node.name
            if name in FORBIDDEN_NAMES or name.split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"{path.name}: {name}")
        if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
            found.append(f"{path.name}: {node.module}")
        if isinstance(node, ast.Attribute) and node.attr == "walk":
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                found.append(f"{path.name}: os.walk")
        if isinstance(node, ast.Constant) and node.value == "rg":
            found.append(f"{path.name}: ripgrep")
    return found


def test_nothing_reads_a_tree_around_the_interface() -> None:
    """Where the trees are, how they are walked, searched and watched is one module."""
    offences = [
        one
        for path in sorted(PACKAGES.glob("*/src/**/*.py"))
        if path.name not in ALLOWED and path.name != "config.py"
        for one in _offences(path)
    ]
    assert offences == []
