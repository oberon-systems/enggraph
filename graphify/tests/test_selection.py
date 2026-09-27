"""What a project prunes: the ignore lines of every level, summed."""

from __future__ import annotations

from tests.test_storage import FakeCursor

from enggraph.config import SETTINGS_PROJECT
from enggraph.selection import resolve


def test_nothing_stored_prunes_nothing() -> None:
    """No level says anything, so only the built-in rules apply."""
    selection = resolve(FakeCursor(), "alpha")
    assert selection.ignore is None
    assert selection.levels == ()


def test_every_level_adds_its_lines() -> None:
    """The global default, the organization and the project all apply."""
    cursor = FakeCursor(
        settings={
            SETTINGS_PROJECT: ".cache-x/\n",
            "beta": "fixtures/\n",
            "alpha": "*.gen.py\n",
        },
        members=[("beta", "alpha")],
    )
    selection = resolve(cursor, "alpha")
    assert selection.ignore is not None
    assert selection.ignore.match_file(".cache-x/one.py")
    assert selection.ignore.match_file("fixtures/two.py")
    assert selection.ignore.match_file("src/three.gen.py")
    assert not selection.ignore.match_file("src/four.py")
    assert [level.origin for level in selection.levels] == [
        "global",
        "organization",
        "project",
    ]


def test_a_document_of_only_comments_is_no_level() -> None:
    """An empty document says nothing and is not listed as a source."""
    cursor = FakeCursor(settings={"alpha": "# nothing yet\n\n"})
    selection = resolve(cursor, "alpha")
    assert selection.ignore is None


def test_organizations_are_summed_in_joining_order() -> None:
    """A project in two organizations inherits the lines of both."""
    cursor = FakeCursor(
        settings={"beta": "one/\n", "gamma": "two/\n"},
        members=[("beta", "alpha"), ("gamma", "alpha")],
    )
    selection = resolve(cursor, "alpha")
    assert [level.name for level in selection.levels] == ["beta", "gamma"]
    assert selection.ignore is not None
    assert selection.ignore.match_file("two/x.py")
