"""What a project selects out of its tree, and what the walk leaves behind.

The walk reaches no database: the ignore spec it filters by is settled by
`enggraph.core.selection` first.
"""

from __future__ import annotations

from pathlib import Path

from dirtree import DirTree

from enggraph.core.selection import to_spec
from enggraph.indexer.discovery import (
    GitIgnores,
    iter_project_files,
    parse_name,
    selects,
    selects_dir,
)


def build(root: Path) -> None:
    """Lay out a tree with a vendored directory in it."""
    (root / "configs").mkdir(parents=True)
    (root / "configs" / "prod.yaml").write_text("a: 1\n")
    (root / "configs" / "thirdparty").mkdir()
    (root / "configs" / "thirdparty" / "third.yaml").write_text("b: 2\n")
    (root / "agents" / "src").mkdir(parents=True)
    (root / "agents" / "src" / "run.py").write_text("x = 1\n")
    (root / "agents" / "notes.txt").write_text("nothing to parse\n")


def selected(root: Path, ignore: list[str] | None = None) -> list[str]:
    """Return the project relative paths, sorted for comparison."""
    spec = to_spec(ignore or [])
    return sorted(iter_project_files(DirTree("alpha", str(root)), spec))


def test_every_path_is_relative_to_the_tree(tmp_path: Path) -> None:
    """A node id is the path the tree holds the file at, and nothing more."""
    build(tmp_path)
    assert selected(tmp_path, ["thirdparty/"]) == [
        "agents/src/run.py",
        "configs/prod.yaml",
    ]


def test_the_ignore_document_prunes_a_whole_directory(tmp_path: Path) -> None:
    """A vendored tree is skipped rather than walked and thrown away."""
    build(tmp_path)
    assert "configs/thirdparty/third.yaml" in selected(tmp_path)
    assert "configs/thirdparty/third.yaml" not in selected(tmp_path, ["thirdparty/"])


def test_a_tree_with_nothing_worth_indexing_selects_nothing(tmp_path: Path) -> None:
    """An empty selection is empty rather than the whole tree."""
    (tmp_path / "notes.txt").write_text("nothing to parse\n")
    assert selected(tmp_path) == []


def test_secrets_are_never_selected(tmp_path: Path) -> None:
    """Whatever the ignore documents say, secrets stay out of the graph."""
    (tmp_path / "server.key").write_text("secret\n")
    (tmp_path / "id_rsa").write_text("#!/bin/sh\n")
    (tmp_path / ".env").write_text("#!/bin/sh\nTOKEN=x\n")
    (tmp_path / "terraform.tfvars").write_text('token = "x"\n')
    (tmp_path / "vault.yml").write_text("password: x\n")
    (tmp_path / "app.sops.yaml").write_text("password: x\n")
    (tmp_path / "credentials.json").write_text("{}\n")
    (tmp_path / "home" / ".ssh").mkdir(parents=True)
    (tmp_path / "home" / ".ssh" / "deploy.yaml").write_text("a: 1\n")
    (tmp_path / "main.tf").write_text('resource "x" "y" {}\n')
    assert selected(tmp_path) == ["main.tf"]
    assert not selects("home/.ssh/deploy.yaml", None)


def test_a_script_is_selected_by_its_shebang(tmp_path: Path) -> None:
    """A script without an extension is read as what its interpreter runs."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "deploy").write_text("#!/usr/bin/env python3\nx = 1\n")
    (tmp_path / "bin" / "setup").write_text("#!/bin/bash -e\necho ok\n")
    (tmp_path / "bin" / "blob").write_text("no shebang here\n")
    (tmp_path / "bin" / "odd").write_text("#!/usr/bin/awk -f\n{ print }\n")
    assert selected(tmp_path) == ["bin/deploy", "bin/setup"]
    tree = DirTree("alpha", str(tmp_path))
    assert parse_name(tree, "bin/deploy") == "bin/deploy.py"
    assert parse_name(tree, "bin/setup") == "bin/setup.sh"


def test_a_file_with_an_extension_is_parsed_as_itself(tmp_path: Path) -> None:
    """A shebang never renames a file that already says what it is."""
    (tmp_path / "run.py").write_text("#!/bin/sh\n")
    assert parse_name(DirTree("alpha", str(tmp_path)), "run.py") == "run.py"


def test_a_watched_path_under_a_skipped_directory_selects_nothing() -> None:
    """Every commit writes under .git, and none of it is worth a re-index."""
    assert not selects(".git/refs/heads/main.py", None)
    assert not selects("node_modules/left-pad/index.js", None)


def test_a_watched_path_obeys_the_ignore_spec() -> None:
    """The rule a walk applies, for a path that arrives without one."""
    ignore = to_spec(["build/"])
    assert selects("src/run.py", ignore)
    assert not selects("src/run.txt", ignore)
    assert not selects("build/run.py", ignore)


def test_a_watched_script_is_selected_by_its_shebang(tmp_path: Path) -> None:
    """The watch reads the first line too, so a new script triggers a run."""
    (tmp_path / "tool").write_text("#!/bin/sh\necho ok\n")
    assert selects("tool", None, DirTree("alpha", str(tmp_path)))


class CountingTree(DirTree):
    """A tree that keeps the files it was asked to read."""

    def __init__(self, project: str, root: object) -> None:
        """Start with nothing read."""
        super().__init__(project, root)
        self.reads: list[str] = []

    def read(self, rel_path: str) -> tuple[str | None, str]:
        """Note the path, then read it."""
        self.reads.append(rel_path)
        return super().read(rel_path)


def python_files(tree: DirTree) -> list[str]:
    """Return the Python files a walk keeps, sorted."""
    return sorted(one for one in iter_project_files(tree, None) if one.endswith(".py"))


def test_a_gitignored_directory_is_never_walked(tmp_path: Path) -> None:
    """Local state the tree ignores stays out, and only a listed .gitignore is read."""
    (tmp_path / ".gitignore").write_text(".runs/\n")
    (tmp_path / ".runs" / "2026").mkdir(parents=True)
    (tmp_path / ".runs" / "2026" / "host.py").write_text("a = 1\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "run.py").write_text("x = 1\n")
    tree = CountingTree("alpha", str(tmp_path))
    assert python_files(tree) == ["src/run.py"]
    assert tree.reads == [".gitignore"]


def test_a_nested_gitignore_holds_below_it_and_can_take_a_file_back(
    tmp_path: Path,
) -> None:
    """The deepest .gitignore with a matching rule decides, as git does."""
    (tmp_path / ".gitignore").write_text("gen_*.py\n")
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        for name in ("local.py", "gen_x.py", "gen_keep.py", "run.py"):
            (tmp_path / folder / name).write_text("x = 1\n")
    (tmp_path / "a" / ".gitignore").write_text("local.py\n!gen_keep.py\n")
    assert python_files(DirTree("alpha", str(tmp_path))) == [
        "a/gen_keep.py",
        "a/run.py",
        "b/local.py",
        "b/run.py",
    ]


def test_a_watched_path_under_a_gitignored_directory_selects_nothing(
    tmp_path: Path,
) -> None:
    """A write into ignored run output is no reason for a run."""
    (tmp_path / ".gitignore").write_text(".runs/\n")
    tree = DirTree("alpha", str(tmp_path))
    assert not selects(".runs/2026/host.py", None, tree)
    assert selects("src/run.py", None, tree)
    assert not selects_dir(".runs/2026", None, GitIgnores(tree))
    assert selects_dir("src", None, GitIgnores(tree))


def test_a_terragrunt_cache_is_skipped(tmp_path: Path) -> None:
    """Terragrunt's copies of modules would shadow the modules themselves."""
    (tmp_path / "live" / ".terragrunt-cache" / "abc").mkdir(parents=True)
    (tmp_path / "live" / ".terragrunt-cache" / "abc" / "main.tf").write_text("")
    (tmp_path / "live" / "main.tf").write_text("")
    assert selected(tmp_path) == ["live/main.tf"]
    assert not selects_dir("live/.terragrunt-cache/abc", None)
