"""Which files of a source are pruned, summed over every level that says so.

What is indexed is every file some producer reads; the only choice left is
what to drop. The ignore documents live in `settings` at three levels -
the global default, the organizations holding the project, the project - and
all of them apply at once.

`enggraph.discovery` walks a directory and reaches no database; this module is
the seam between the two, so the walk stays testable against a bare tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pathspec
from psycopg2.extensions import cursor as Cursor

from enggraph.config import SETTINGS_PROJECT
from enggraph.discovery import to_spec
from enggraph.storage import list_memberships, read_ignore

Origin = Literal["project", "organization", "global"]


@dataclass(frozen=True)
class Level:
    """One level's ignore document, as stored."""

    origin: Origin
    name: str
    document: str


@dataclass(frozen=True)
class Selection:
    """A project's summed ignore spec, and the levels it was summed from."""

    ignore: pathspec.PathSpec | None
    levels: tuple[Level, ...]


def levels(cursor: Cursor, project: str) -> list[tuple[Origin, str]]:
    """Return the (origin, project) rows to read, most specific first.

    Every level-resolved setting walks this; the ignore lines are summed from
    the global default down instead.
    """
    scopes: list[tuple[Origin, str]] = [("project", project)]
    for organization in list_memberships(cursor, project):
        scopes.append(("organization", organization))
    scopes.append(("global", SETTINGS_PROJECT))
    return scopes


def resolve(cursor: Cursor, project: str) -> Selection:
    """Sum every level's ignore document into the one spec the walk uses."""
    scopes = levels(cursor, project)
    found: list[Level] = []
    for origin, name in [scopes[-1], *scopes[1:-1], scopes[0]]:
        document = read_ignore(cursor, name)
        if document and document.strip():
            found.append(Level(origin, name, document))
    lines = [line for level in found for line in level.document.splitlines()]
    return Selection(to_spec(lines), tuple(found))
