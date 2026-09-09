"""Which files of a source are indexed, and where that answer came from.

The selection used to be two files in the tree being indexed: `.enggraph-keep`
naming what becomes a node and `.enggraph-ignore` naming what is pruned, under
the `.ctxkeep` and `.ctxignore` those two were called before. Every mount is
read-only by contract, so nothing in this stack could edit them - changing what
a project indexes meant editing the repository being indexed.

The documents live in `project_settings` now, resolved most specific first:
the directory, then the project, then the global default. A file still in the
tree beats all three. That is not a transitional courtesy: a repository that
ships one has said what it wants indexed, in the place a reader of that
repository will look, and the database silently overruling it would make the
graph disagree with the tree for reasons visible nowhere.

`enggraph.discovery` walks a directory and reaches no database; this module is
the seam between the two, so the walk stays testable against a bare tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pathspec
from psycopg2.extensions import cursor as Cursor

from enggraph.config import IGNORE_FILES, KEEP_FILES, SETTINGS_PROJECT
from enggraph.discovery import SpecPair, load_spec, present, to_spec
from enggraph.identifiers import source_mount
from enggraph.storage import list_memberships, read_settings

# Where a document was read from. `default` is the absence of one: no keep
# list means the built-in extension set, no ignore list means the built-in
# directory skip list and nothing else.
Origin = Literal["file", "directory", "project", "organization", "global", "default"]

# The database levels, most specific first. The empty alias is the project
# level, and for a project mounted whole it is also its only directory - one
# row, because for that project the two are the same thing.
DB_ORIGINS: tuple[Origin, ...] = (
    "directory",
    "project",
    "organization",
    "global",
)


@dataclass(frozen=True)
class Selection:
    """One source's two specs, and where each of them came from."""

    keep: pathspec.PathSpec | None
    ignore: pathspec.PathSpec | None
    keep_origin: Origin
    ignore_origin: Origin

    @property
    def specs(self) -> SpecPair:
        """The pair `enggraph.discovery` walks with, keep first."""
        return self.keep, self.ignore


def levels(cursor: Cursor, project: str, alias: str) -> list[tuple[Origin, str, str]]:
    """Return the (origin, project, alias) rows to read, most specific first.

    A project mounted whole has one row rather than two, so asking for its
    directory and its project level would read the same row twice and report
    the more specific origin for what is really the project's own setting.

    An organization sits between a project and the global default: what it
    sets is what its members read unless they say otherwise, which is what
    makes a set of projects configurable as one. A project belonging to
    several is asked in the order it joined them, so the first organization
    that answers does - and its own row still beats all of them.
    """
    scopes: list[tuple[Origin, str, str]] = []
    if alias:
        scopes.append(("directory", project, alias))
    scopes.append(("project", project, ""))
    for organization in list_memberships(cursor, project):
        scopes.append(("organization", organization, ""))
    scopes.append(("global", SETTINGS_PROJECT, ""))
    return scopes


def stored(
    cursor: Cursor, project: str, alias: str
) -> dict[str, tuple[Origin, pathspec.PathSpec]]:
    """Read the database levels once, keeping the first answer for each half.

    Both documents are resolved independently: a project may store a keep list
    of its own while its ignore list comes from the global default, exactly as
    a tree may ship one file and not the other. A document is parsed before it
    counts as an answer, because one holding nothing but comments selects
    nothing - which is the built-in default, not an empty list.
    """
    found: dict[str, tuple[Origin, pathspec.PathSpec]] = {}
    for origin, name, key in levels(cursor, project, alias):
        keep, ignore = read_settings(cursor, name, key)
        for half, document in (("keep", keep), ("ignore", ignore)):
            if half in found or document is None:
                continue
            spec = to_spec(document.splitlines())
            if spec is not None:
                found[half] = (origin, spec)
    return found


def resolve(cursor: Cursor, project: str, alias: str, root_path: str) -> Selection:
    """Settle one source's selection, and say where each half came from.

    `root_path` is the directory as this process can read it - the mount, not
    the host path - because the file that beats the database is the one the
    walk would have found.
    """
    documents = stored(cursor, project, alias)
    resolved: dict[str, tuple[pathspec.PathSpec | None, Origin]] = {}
    for half, file_names in (("keep", KEEP_FILES), ("ignore", IGNORE_FILES)):
        file_name = present(root_path, file_names)
        spec = load_spec(root_path, file_name) if file_name else None
        if spec is not None:
            resolved[half] = (spec, "file")
            continue
        if half in documents:
            origin, stored_spec = documents[half]
            resolved[half] = (stored_spec, origin)
            continue
        resolved[half] = (None, "default")
    keep, keep_origin = resolved["keep"]
    ignore, ignore_origin = resolved["ignore"]
    return Selection(keep, ignore, keep_origin, ignore_origin)


def resolve_all(
    cursor: Cursor, project: str, aliases: list[str]
) -> list[tuple[str, Selection]]:
    """Resolve every source of a project, in the order it reads them."""
    return [
        (alias, resolve(cursor, project, alias, source_mount(project, alias)))
        for alias in aliases
    ]
