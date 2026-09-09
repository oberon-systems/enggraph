"""What a project may be called, and what it may not."""

from __future__ import annotations

import pytest

from enggraph.identifiers import project_mount, project_name


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
