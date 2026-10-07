"""The formats a tree holds, as a project records them."""

from __future__ import annotations

from pathlib import Path

from test_storage import FakeCursor

from enggraph.core import trees
from enggraph.indexer import formats


def build(root: Path) -> None:
    """Lay out a tree holding code, a Makefile, a Dockerfile variant and a script."""
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "Makefile").write_text("all:\n\ttrue\n")
    (root / "Dockerfile.ci").write_text("FROM scratch\n")
    (root / "deploy").write_text("#!/usr/bin/env bash\necho ok\n")
    (root / "notes.txt").write_text("not a format anyone reads\n")


def test_a_scan_names_every_format_it_finds(tmp_path: Path) -> None:
    """Extensions, names a parser answers to, and scripts by interpreter."""
    build(tmp_path)
    cursor = FakeCursor()
    found = formats.scan(cursor, "alpha", trees.at(str(tmp_path)))
    assert found == ["#!bash", ".py", "dockerfile.*", "makefile"]


def test_a_later_scan_only_adds(tmp_path: Path) -> None:
    """A format that left the tree stays listed; a new one is added."""
    build(tmp_path)
    cursor = FakeCursor()
    formats.scan(cursor, "alpha", trees.at(str(tmp_path)))
    (tmp_path / "src" / "app.py").unlink()
    (tmp_path / "main.tf").write_text('resource "x" "y" {}\n')
    assert ".py" in formats.scan(cursor, "alpha", trees.at(str(tmp_path)))
    assert ".tf" in cursor.formats["alpha"]


def test_an_ignored_directory_contributes_no_format(tmp_path: Path) -> None:
    """What the ignore lines prune is not a format of the project."""
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.rb").write_text("x = 1\n")
    cursor = FakeCursor(settings={"alpha": "vendor/\n"})
    assert formats.scan(cursor, "alpha", trees.at(str(tmp_path))) == []
