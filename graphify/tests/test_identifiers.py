"""What a project may be called, and what it may not."""

from __future__ import annotations

import os
import pathlib

import pytest

from enggraph import identifiers
from enggraph.identifiers import is_mounted, project_mount, project_name


def test_derives_the_name_from_the_last_path_segment() -> None:
    """Naming a neighbour is meant to be a matter of naming its directory."""
    assert project_name("", "/home/user/src/alpha") == "alpha"
    assert project_name("", "/home/user/src/alpha/") == "alpha"


def test_an_explicit_name_wins_and_is_cleaned() -> None:
    """A name travels in a URL path segment, so it is narrowed to what fits."""
    assert project_name("My Repo", "/home/user/src/alpha") == "my-repo"


def test_reserves_the_builtin_prefix() -> None:
    """Without this the cleaning turns `_memory` into `memory`, silently."""
    with pytest.raises(RuntimeError, match="reserved"):
        project_name("_memory", "/home/user/src/whatever")
    with pytest.raises(RuntimeError, match="reserved"):
        project_name("", "/home/user/src/_common")


def test_refuses_a_name_that_would_climb_the_tree() -> None:
    """A name is also a directory under the extractor cache root."""
    for root in ("/", "/home/user/src/.", "/home/user/src/.."):
        with pytest.raises(RuntimeError):
            project_name("", root)


def test_a_project_is_mounted_under_its_own_name() -> None:
    """The compose override is generated from the same name."""
    assert project_mount("alpha") == "/code/alpha"


def test_a_live_directory_is_mounted(tmp_path: pathlib.Path) -> None:
    """The ordinary case."""
    assert is_mounted(str(tmp_path))


def test_a_missing_path_or_a_file_is_not_mounted(tmp_path: pathlib.Path) -> None:
    """Nothing there, or something that is not a tree."""
    (tmp_path / "beta").write_text("")
    assert not is_mounted(str(tmp_path / "alpha"))
    assert not is_mounted(str(tmp_path / "beta"))


def test_a_deleted_directory_behind_a_mount_is_not_mounted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind mount keeps a replaced host directory: empty, with no links."""
    found = os.stat(tmp_path)
    fields = list(found)
    fields[3] = 0
    monkeypatch.setattr(identifiers.os, "stat", lambda path: os.stat_result(fields))
    assert not is_mounted(str(tmp_path))
