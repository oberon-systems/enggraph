"""Decide which files of the mounted project are worth indexing."""

from __future__ import annotations

import logging
import os
import posixpath
from collections.abc import Iterator

import pathspec

from enggraph.config import (
    DEFAULT_IGNORED_DIRS,
    INTERPRETER_EXTENSIONS,
    MAX_FILE_BYTES,
    SECRET_PATTERNS,
)
from enggraph.parsers import is_default_source

LOG = logging.getLogger(__name__)

SECRETS = pathspec.PathSpec.from_lines("gitwildmatch", SECRET_PATTERNS)


def to_spec(lines: list[str]) -> pathspec.PathSpec | None:
    """Build a PathSpec from the lines of an ignore document.

    A document holding only comments and blank lines is None rather than an
    empty spec, which is what makes it identical to no document at all.
    """
    patterns = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns) if patterns else None


def interpreter(full_path: str) -> str:
    """Return the interpreter a shebang line names, lowercased, or ''."""
    try:
        if not 0 < os.path.getsize(full_path) <= MAX_FILE_BYTES:
            return ""
        with open(full_path, "rb") as handle:
            first = handle.readline(200)
    except OSError:
        return ""
    if not first.startswith(b"#!"):
        return ""
    tokens = [
        token
        for token in first[2:].decode("utf-8", "ignore").split()
        if not token.startswith("-")
    ]
    if not tokens:
        return ""
    name = posixpath.basename(tokens[0])
    if name == "env" and len(tokens) > 1:
        name = posixpath.basename(tokens[1])
    return name.lower()


def shebang_extension(full_path: str, rel_path: str) -> str:
    """Return the extension a script without one is read as, or ''."""
    if posixpath.splitext(posixpath.basename(rel_path))[1]:
        return ""
    return INTERPRETER_EXTENSIONS.get(interpreter(full_path), "")


def parse_name(full_path: str, rel_path: str) -> str:
    """Return the path a parser is chosen by: a script gets its shebang's suffix."""
    return rel_path + shebang_extension(full_path, rel_path)


def ignored(rel_path: str, ignore_spec: pathspec.PathSpec | None) -> bool:
    """Whether key material or the ignore documents drop this file."""
    if SECRETS.match_file(rel_path):
        return True
    return ignore_spec is not None and ignore_spec.match_file(rel_path)


def iter_project_files(
    mount: str, ignore_spec: pathspec.PathSpec | None
) -> Iterator[tuple[str, str]]:
    """Yield (absolute path, project relative path) for a project's tree.

    The ignore spec is settled by `enggraph.selection` before the walk, so
    nothing here reads a database.
    """
    yield from walk_selected(mount, ignore_spec)


def walk_selected(
    root_path: str, ignore_spec: pathspec.PathSpec | None
) -> Iterator[tuple[str, str]]:
    """Yield every supported file the ignore spec leaves in."""
    for current_dir, dir_names, file_names in os.walk(root_path):
        rel_dir = os.path.relpath(current_dir, root_path)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dir_names[:] = [
            name
            for name in sorted(dir_names)
            if name not in DEFAULT_IGNORED_DIRS
            and not (
                ignore_spec is not None
                and ignore_spec.match_file(f"{posixpath.join(rel_dir, name)}/")
            )
        ]
        for file_name in sorted(file_names):
            rel_path = posixpath.join(rel_dir, file_name)
            full_path = os.path.join(current_dir, file_name)
            if selects_file(rel_path, ignore_spec, full_path):
                yield full_path, rel_path


def selects_file(
    rel_path: str,
    ignore_spec: pathspec.PathSpec | None,
    full_path: str | None = None,
) -> bool:
    """Whether a file is indexed, the directories already settled.

    A file some producer reads is, and so is a script without an extension
    whose shebang names an interpreter one of them reads.
    """
    if ignored(rel_path, ignore_spec):
        return False
    if is_default_source(posixpath.basename(rel_path)):
        return True
    return full_path is not None and bool(shebang_extension(full_path, rel_path))


def selects(
    rel_path: str,
    ignore_spec: pathspec.PathSpec | None,
    full_path: str | None = None,
) -> bool:
    """Whether a path the walk never produced would have been selected.

    A watcher is told about a file rather than arriving at it, so the pruning
    `walk_selected` does on the way down has to be applied to the path itself:
    the default skip list and the ignore spec both speak about directories,
    and without this a project would be re-indexed for every write under .git.
    """
    parts = rel_path.split("/")
    prefix = ""
    for part in parts[:-1]:
        if part in DEFAULT_IGNORED_DIRS:
            return False
        prefix = posixpath.join(prefix, part)
        if ignore_spec is not None and ignore_spec.match_file(f"{prefix}/"):
            return False
    return selects_file(rel_path, ignore_spec, full_path)


def read_source(full_path: str, rel_path: str) -> tuple[str | None, str]:
    """Return the text of a file, and why it was skipped when it has no text.

    The reason travels with the content because a skipped file is a file the
    graph will not describe, and the run reports that at the end rather than
    leaving a node missing with no account of it.
    """
    try:
        size = os.path.getsize(full_path)
    except OSError:
        LOG.exception("Failed to stat %s", rel_path)
        return None, "unreadable"
    if size > MAX_FILE_BYTES:
        LOG.info("Skipping %s: %d bytes exceeds the limit", rel_path, size)
        return None, "over the size limit"
    try:
        with open(full_path, encoding="utf-8", errors="ignore") as handle:
            return handle.read(), ""
    except OSError:
        LOG.exception("Failed to read %s", rel_path)
        return None, "unreadable"
