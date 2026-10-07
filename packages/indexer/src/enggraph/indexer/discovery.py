"""Decide which files of the mounted project are worth indexing."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

import pathspec

from enggraph.core.config import (
    DEFAULT_IGNORED_DIRS,
    INTERPRETER_EXTENSIONS,
    MAX_FILE_BYTES,
    SECRET_PATTERNS,
)
from enggraph.core.trees import Tree
from enggraph.indexer.parsers import is_default_source

SECRETS = pathspec.PathSpec.from_lines("gitwildmatch", SECRET_PATTERNS)


def interpreter(tree: Tree, rel_path: str) -> str:
    """Return the interpreter a shebang line names, lowercased, or ''."""
    size = tree.size(rel_path)
    if size is None or not 0 < size <= MAX_FILE_BYTES:
        return ""
    first = tree.head(rel_path, 200).split(b"\n", 1)[0]
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


def shebang_extension(tree: Tree, rel_path: str) -> str:
    """Return the extension a script without one is read as, or ''."""
    if posixpath.splitext(posixpath.basename(rel_path))[1]:
        return ""
    return INTERPRETER_EXTENSIONS.get(interpreter(tree, rel_path), "")


def parse_name(tree: Tree, rel_path: str) -> str:
    """Return the path a parser is chosen by: a script gets its shebang's suffix."""
    return rel_path + shebang_extension(tree, rel_path)


def ignored(rel_path: str, ignore_spec: pathspec.PathSpec | None) -> bool:
    """Whether key material or the ignore documents drop this file."""
    if SECRETS.match_file(rel_path):
        return True
    return ignore_spec is not None and ignore_spec.match_file(rel_path)


def iter_project_files(
    tree: Tree, ignore_spec: pathspec.PathSpec | None
) -> Iterator[str]:
    """Yield the project relative path of every file a project's tree gives.

    The ignore spec is settled by `enggraph.core.selection` before the walk, so
    nothing here reads a database.
    """
    yield from walk_selected(tree, ignore_spec)


def walk_selected(tree: Tree, ignore_spec: pathspec.PathSpec | None) -> Iterator[str]:
    """Yield every supported file the ignore spec leaves in."""

    def keep_dir(rel_dir: str) -> bool:
        return posixpath.basename(rel_dir) not in DEFAULT_IGNORED_DIRS and not ignored(
            f"{rel_dir}/", ignore_spec
        )

    def keep_file(rel_path: str) -> bool:
        return selects_file(rel_path, ignore_spec, tree)

    yield from tree.walk("", keep_dir, keep_file)


def selects_file(
    rel_path: str,
    ignore_spec: pathspec.PathSpec | None,
    tree: Tree | None = None,
) -> bool:
    """Whether a file is indexed, the directories already settled.

    A file some producer reads is, and so is a script without an extension
    whose shebang names an interpreter one of them reads.
    """
    if ignored(rel_path, ignore_spec):
        return False
    if is_default_source(posixpath.basename(rel_path)):
        return True
    return tree is not None and bool(shebang_extension(tree, rel_path))


def selects(
    rel_path: str,
    ignore_spec: pathspec.PathSpec | None,
    tree: Tree | None = None,
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
        if ignored(f"{prefix}/", ignore_spec):
            return False
    return selects_file(rel_path, ignore_spec, tree)
