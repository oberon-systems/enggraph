"""The formats a project's tree holds, as the project records them.

A format is an extension, a file name a parser answers to, or `#!<name>` for a
script without an extension. The list only grows: a run adds what it found.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable

from psycopg2.extensions import cursor as Cursor

from enggraph.discovery import interpreter, iter_project_files
from enggraph.parsers.registry import FILENAME_PARSERS
from enggraph.selection import resolve
from enggraph.storage import add_formats


def format_of(full_path: str, rel_path: str) -> str:
    """Name the format of one selected file."""
    name = posixpath.basename(rel_path).lower()
    if name in FILENAME_PARSERS:
        return name
    if name.startswith("dockerfile."):
        return "dockerfile.*"
    extension = posixpath.splitext(name)[1]
    return extension or f"#!{interpreter(full_path)}"


def found(files: Iterable[tuple[str, str]]) -> list[str]:
    """Return the formats among (absolute path, relative path) pairs, sorted."""
    return sorted({format_of(full_path, rel_path) for full_path, rel_path in files})


def record(cursor: Cursor, project: str, files: Iterable[tuple[str, str]]) -> list[str]:
    """Add the formats of files already walked to the project's list."""
    return add_formats(cursor, project, found(files))


def scan(cursor: Cursor, project: str, mount: str) -> list[str]:
    """Walk a project's tree and add every format it holds to its list."""
    selection = resolve(cursor, project)
    return record(cursor, project, iter_project_files(mount, selection.ignore))
