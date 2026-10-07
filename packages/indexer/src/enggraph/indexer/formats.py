"""The formats a project's tree holds, as the project records them.

A format is an extension, a file name a parser answers to, or `#!<name>` for a
script without an extension. The list only grows: a run adds what it found.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable

from psycopg2.extensions import cursor as Cursor

from enggraph.core.selection import resolve
from enggraph.core.storage import add_formats
from enggraph.core.trees import Tree
from enggraph.indexer.discovery import interpreter, iter_project_files
from enggraph.indexer.parsers.registry import FILENAME_PARSERS


def format_of(tree: Tree, rel_path: str) -> str:
    """Name the format of one selected file."""
    name = posixpath.basename(rel_path).lower()
    if name in FILENAME_PARSERS:
        return name
    if name.startswith("dockerfile."):
        return "dockerfile.*"
    extension = posixpath.splitext(name)[1]
    return extension or f"#!{interpreter(tree, rel_path)}"


def found(tree: Tree, files: Iterable[str]) -> list[str]:
    """Return the formats among a tree's relative paths, sorted."""
    return sorted({format_of(tree, rel_path) for rel_path in files})


def record(cursor: Cursor, project: str, tree: Tree, files: Iterable[str]) -> list[str]:
    """Add the formats of files already walked to the project's list."""
    return add_formats(cursor, project, found(tree, files))


def scan(cursor: Cursor, project: str, tree: Tree) -> list[str]:
    """Walk a project's tree and add every format it holds to its list."""
    selection = resolve(cursor, project)
    return record(cursor, project, tree, iter_project_files(tree, selection.ignore))
