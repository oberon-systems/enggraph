"""Where one source reads its selection from, and what wins when several say.

The order is a file in the tree, then the directory row, then the project row,
then the global default, then the built-in selection. Both documents resolve
independently, because a tree may ship one file and not the other - and the
origin is reported alongside the spec, since it is what the dashboard shows
and what an index run records.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_storage import FakeCursor

from enggraph.config import (
    IGNORE_FILE,
    IGNORE_FILES,
    KEEP_FILE,
    KEEP_FILES,
    SETTINGS_PROJECT,
)
from enggraph.selection import resolve


def origins(tmp_path: Path, cursor: FakeCursor) -> tuple[str, str]:
    """Resolve one source of `mono` and return the two origins."""
    selection = resolve(cursor, "mono", "configs", str(tmp_path))
    return selection.keep_origin, selection.ignore_origin


def test_a_tree_with_nothing_falls_back_to_the_built_in_selection(
    tmp_path: Path,
) -> None:
    """No file and no row is the default: no spec at all, either half."""
    selection = resolve(FakeCursor(), "mono", "configs", str(tmp_path))
    assert (selection.keep, selection.ignore) == (None, None)
    assert origins(tmp_path, FakeCursor()) == ("default", "default")


def test_the_global_default_answers_when_nothing_else_does(tmp_path: Path) -> None:
    """Every project falls back to the one row under _settings."""
    cursor = FakeCursor(settings={(SETTINGS_PROJECT, ""): ("*.py\n", "*.pem\n")})
    assert origins(tmp_path, cursor) == ("global", "global")


def test_the_project_row_beats_the_global_default(tmp_path: Path) -> None:
    """A project that has said what it indexes is not overruled by a default."""
    cursor = FakeCursor(
        settings={
            (SETTINGS_PROJECT, ""): ("*.py\n", "*.pem\n"),
            ("mono", ""): ("*.md\n", None),
        }
    )
    # The ignore half has nothing at the project level, so it keeps falling.
    assert origins(tmp_path, cursor) == ("project", "global")


def test_the_directory_row_beats_the_project_row(tmp_path: Path) -> None:
    """A slice of a monorepo says what it indexes without the rest agreeing."""
    cursor = FakeCursor(
        settings={
            ("mono", ""): ("*.md\n", None),
            ("mono", "configs"): ("*.yaml\n", None),
        }
    )
    assert origins(tmp_path, cursor) == ("directory", "default")


def test_an_organization_answers_for_its_members(tmp_path: Path) -> None:
    """What a set of projects is configured as, in one row instead of many."""
    cursor = FakeCursor(
        settings={("acme", ""): ("*.hcl\n", None)},
        members=[("acme", "mono")],
    )
    assert origins(tmp_path, cursor)[0] == "organization"


def test_a_project_of_its_own_beats_the_organization(tmp_path: Path) -> None:
    """Inheriting is a fallback, not a rule imposed on a member."""
    cursor = FakeCursor(
        settings={("acme", ""): ("*.hcl\n", None), ("mono", ""): ("*.py\n", None)},
        members=[("acme", "mono")],
    )
    assert origins(tmp_path, cursor)[0] == "project"


def test_the_first_organization_that_answers_does(tmp_path: Path) -> None:
    """A project belongs to several, and they are asked in joining order."""
    cursor = FakeCursor(
        settings={("infra", ""): ("*.tf\n", None)},
        members=[("acme", "mono"), ("infra", "mono")],
    )
    assert origins(tmp_path, cursor)[0] == "organization"


def test_an_organization_still_loses_to_the_directory(tmp_path: Path) -> None:
    """The order is unchanged below it: the slice decides for itself."""
    cursor = FakeCursor(
        settings={
            ("acme", ""): ("*.hcl\n", None),
            ("mono", "configs"): ("*.yaml\n", None),
        },
        members=[("acme", "mono")],
    )
    assert origins(tmp_path, cursor)[0] == "directory"


def test_a_file_in_the_tree_beats_every_stored_row(tmp_path: Path) -> None:
    """A repository that ships a pair keeps deciding its own index."""
    (tmp_path / KEEP_FILE).write_text("*.tf\n")
    cursor = FakeCursor(
        settings={
            (SETTINGS_PROJECT, ""): ("*.py\n", "*.pem\n"),
            ("mono", "configs"): ("*.yaml\n", "*.key\n"),
        }
    )
    keep_origin, ignore_origin = origins(tmp_path, cursor)
    assert keep_origin == "file"
    # Only the half the tree actually holds. The other one still falls.
    assert ignore_origin == "directory"


def test_the_old_file_names_are_still_read(tmp_path: Path) -> None:
    """A tree shipping the pre-rename pair keeps deciding its own index."""
    (tmp_path / KEEP_FILES[1]).write_text("*.tf\n")
    (tmp_path / IGNORE_FILES[1]).write_text("vendor/\n")
    cursor = FakeCursor(settings={(SETTINGS_PROJECT, ""): ("*.py\n", "*.pem\n")})
    assert origins(tmp_path, cursor) == ("file", "file")


def test_the_current_file_names_win_over_the_old_ones(tmp_path: Path) -> None:
    """A tree carrying both pairs is read under the name it wrote last."""
    (tmp_path / KEEP_FILES[0]).write_text("*.tf\n")
    (tmp_path / KEEP_FILES[1]).write_text("*.md\n")
    cursor = FakeCursor(settings={})
    selection = resolve(cursor, "mono", "configs", str(tmp_path))
    assert selection.keep is not None
    assert selection.keep.match_file("main.tf")
    assert not selection.keep.match_file("README.md")


def test_each_half_resolves_on_its_own(tmp_path: Path) -> None:
    """A tree may ship one file and not the other, and often does."""
    (tmp_path / IGNORE_FILE).write_text("vendor/\n")
    cursor = FakeCursor(settings={("mono", ""): ("*.md\n", None)})
    assert origins(tmp_path, cursor) == ("project", "file")


def test_a_document_of_only_comments_says_nothing(tmp_path: Path) -> None:
    """An empty spec would select nothing; the level has to keep falling."""
    cursor = FakeCursor(
        settings={
            ("mono", "configs"): ("# nothing yet\n\n", None),
            ("mono", ""): ("*.md\n", None),
        }
    )
    assert origins(tmp_path, cursor) == ("project", "default")


def test_a_project_mounted_whole_reads_one_row_rather_than_two(
    tmp_path: Path,
) -> None:
    """The empty alias is the project level and its only directory at once."""
    cursor = FakeCursor(settings={("alpha", ""): ("*.py\n", None)})
    selection = resolve(cursor, "alpha", "", str(tmp_path))
    assert selection.keep_origin == "project"
    assert selection.specs == (selection.keep, selection.ignore)
