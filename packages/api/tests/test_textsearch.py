"""File text is read and searched on the mounts, never kept in the database."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from enggraph.api import textsearch
from enggraph.core import sources, trees

CONFIG = "instances:\n  web-01.example.com:\n    flavor: small\n"


def test_a_loose_pattern_ignores_what_separates_the_words() -> None:
    """Only the letters and digits must agree, in order."""
    pattern = re.compile(textsearch.loose_pattern("web_01_example_com"), re.I)
    assert pattern.search("  web-01.example.com:")
    assert pattern.search("WEB 01 example com")
    assert not pattern.search("web-02.example.com")
    assert textsearch.loose_pattern(" -._ ") == ""


def test_ranges_are_read_from_the_file_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two pieces of one file cost one read; a missing file says why."""
    reads: list[str] = []

    def read(project: str, path: str, limit: int = 0) -> tuple[str | None, str]:
        reads.append(path)
        if path == "gone.yaml":
            return None, "no such file"
        return CONFIG, ""

    monkeypatch.setattr(textsearch.sources, "read", read)
    answered = textsearch.read_ranges(
        [
            textsearch.Range("alpha", "web.yaml", 1, 2),
            textsearch.Range("alpha", "web.yaml", 3, 3),
            textsearch.Range("alpha", "gone.yaml", 1, 1),
        ]
    )
    assert [one["text"] for one in answered] == [
        "instances:\n  web-01.example.com:",
        "    flavor: small",
        None,
    ]
    assert answered[2]["reason"] == "no such file"
    assert reads == ["web.yaml", "gone.yaml"]


def test_a_ripgrep_line_is_read_whatever_its_path_holds() -> None:
    """The path ends at a NUL, so a colon in a file name breaks nothing."""
    assert trees._parse("./a:b.yaml\x003:  web-01: x") == (
        "a:b.yaml",
        3,
        "  web-01: x",
    )
    assert trees._parse("no separators") is None


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_grep_reads_the_tree_as_the_indexer_selects_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ignored, skipped and denied files are left out; the rest answer by line."""
    tree = tmp_path / "alpha"
    (tree / "infra").mkdir(parents=True)
    (tree / "infra" / "web.yaml").write_text(CONFIG)
    (tree / "build").mkdir()
    (tree / "build" / "web.yaml").write_text(CONFIG)
    (tree / ".git").mkdir()
    (tree / ".git" / "config").write_text(CONFIG)
    (tree / ".env").write_text("HOST=web-01.example.com\n")
    monkeypatch.setattr(trees, "CODE_ROOT", str(tmp_path))

    found = textsearch.grep(
        "alpha",
        textsearch.loose_pattern("web_01_example_com"),
        ["build/"],
        None,
        10,
    )
    assert found == [
        {
            "project": "alpha",
            "path": "infra/web.yaml",
            "line": 2,
            "text": "  web-01.example.com:",
        }
    ]
    assert textsearch.grep("alpha", "web-01.", [], None, 10, fixed=True)


def test_ranges_never_hand_over_a_denied_file_or_climb_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A context packet comes through here, so this is what no tool can get past."""
    tree = tmp_path / "alpha"
    tree.mkdir()
    (tree / "app.py").write_text("visible\n")
    (tree / ".env").write_text("TOKEN=alpha-fake-value\n")
    (tree / "server.key").write_text("alpha-fake-key\n")
    (tmp_path / "outside.txt").write_text("not yours\n")
    (tree / "escape.txt").symlink_to(tmp_path / "outside.txt")
    monkeypatch.setattr(trees, "CODE_ROOT", str(tmp_path))
    wanted = ["app.py", ".env", "server.key", "../outside.txt", "escape.txt"]
    answered = textsearch.read_ranges(
        [textsearch.Range("alpha", path, 1, 1) for path in wanted]
    )
    assert [one["text"] for one in answered] == ["visible", None, None, None, None]
    assert [one["reason"] for one in answered[1:]] == [
        sources.DENIED,
        sources.DENIED,
        sources.ESCAPES,
        sources.ESCAPES,
    ]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_a_search_finds_no_secret_and_nothing_outside_whatever_the_glob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The deny list holds with a glob naming the file, and no glob leaves the tree."""
    tree = tmp_path / "alpha"
    tree.mkdir()
    (tree / "app.py").write_text("needle\n")
    (tree / ".env").write_text("needle\n")
    (tree / "server.key").write_text("needle\n")
    (tmp_path / "outside.txt").write_text("needle\n")
    monkeypatch.setattr(trees, "CODE_ROOT", str(tmp_path))
    for path_glob, expected in (
        (None, ["app.py"]),
        (".env", []),
        ("*.key", []),
        ("../*", []),
        ("../outside.txt", []),
        (str(tmp_path / "outside.txt"), []),
    ):
        found = textsearch.grep("alpha", "needle", [], path_glob, 10, fixed=True)
        assert [one["path"] for one in found] == expected, path_glob
