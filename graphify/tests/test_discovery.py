"""What a project selects out of its tree, and what the walk leaves behind.

The walk reaches no database: the ignore spec it filters by is settled by
`enggraph.selection` first.
"""

from __future__ import annotations

from pathlib import Path

from enggraph.discovery import iter_project_files, parse_name, selects, to_spec


def build(root: Path) -> None:
    """Lay out a mount holding a tree with a vendored directory in it."""
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
    return sorted(rel for _, rel in iter_project_files(str(root), spec))


def test_every_path_is_relative_to_the_mount(tmp_path: Path) -> None:
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
    """An empty selection is empty rather than the whole mount."""
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
    assert parse_name(str(tmp_path / "bin" / "deploy"), "bin/deploy") == (
        "bin/deploy.py"
    )
    assert parse_name(str(tmp_path / "bin" / "setup"), "bin/setup") == "bin/setup.sh"


def test_a_file_with_an_extension_is_parsed_as_itself(tmp_path: Path) -> None:
    """A shebang never renames a file that already says what it is."""
    (tmp_path / "run.py").write_text("#!/bin/sh\n")
    assert parse_name(str(tmp_path / "run.py"), "run.py") == "run.py"


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
    assert selects("tool", None, str(tmp_path / "tool"))
