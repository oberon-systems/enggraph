"""Decide which files of a project's tree are worth indexing."""

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

SECRETS = pathspec.PathSpec.from_lines("gitignore", SECRET_PATTERNS)
GITIGNORE = ".gitignore"


class GitIgnores:
    """A tree's own .gitignore files, each read once, applied as git applies them.

    What they name is local state or secrets, so they are always honoured.
    """

    def __init__(self, tree: Tree) -> None:
        """Take the tree the files are read from."""
        self.tree = tree
        self.rules: dict[str, pathspec.GitIgnoreSpec | None] = {}

    def enter(self, rel_dir: str, file_names: list[str]) -> None:
        """Note a walked directory, reading its .gitignore only when it lists one."""
        if rel_dir not in self.rules:
            self.rules[rel_dir] = (
                self._read(rel_dir) if GITIGNORE in file_names else None
            )

    def forget(self, rel_dir: str) -> None:
        """Drop what was read for a directory, so its .gitignore is read again."""
        self.rules.pop(rel_dir, None)

    def ignores(self, rel_path: str, is_dir: bool = False) -> bool:
        """Whether git ignores a path: the deepest file with a matching rule decides."""
        parts = rel_path.split("/")
        verdict = False
        for depth in range(len(parts)):
            spec = self._spec("/".join(parts[:depth]))
            if spec is None:
                continue
            below = "/".join(parts[depth:]) + ("/" if is_dir else "")
            include = spec.check_file(below).include
            if include is not None:
                verdict = include
        return verdict

    def _spec(self, rel_dir: str) -> pathspec.GitIgnoreSpec | None:
        if rel_dir not in self.rules:
            self.rules[rel_dir] = self._read(rel_dir)
        return self.rules[rel_dir]

    def _read(self, rel_dir: str) -> pathspec.GitIgnoreSpec | None:
        text, _ = self.tree.read(posixpath.join(rel_dir, GITIGNORE))
        if not text:
            return None
        spec = pathspec.GitIgnoreSpec.from_lines(text.splitlines())
        return spec if spec.patterns else None


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
    """Yield every supported file the ignore spec and the tree's .gitignore leave in."""
    gitignores = GitIgnores(tree)

    def keep_dir(rel_dir: str) -> bool:
        return (
            posixpath.basename(rel_dir) not in DEFAULT_IGNORED_DIRS
            and not ignored(f"{rel_dir}/", ignore_spec)
            and not gitignores.ignores(rel_dir, is_dir=True)
        )

    def keep_file(rel_path: str) -> bool:
        return not gitignores.ignores(rel_path) and selects_file(
            rel_path, ignore_spec, tree
        )

    yield from tree.walk("", keep_dir, keep_file, gitignores.enter)


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
    if is_default_source(rel_path):
        return True
    return tree is not None and bool(shebang_extension(tree, rel_path))


def selects(
    rel_path: str,
    ignore_spec: pathspec.PathSpec | None,
    tree: Tree | None = None,
    gitignores: GitIgnores | None = None,
) -> bool:
    """Whether a path the walk never produced would have been selected.

    A watcher is told about a file rather than arriving at it, so the pruning
    `walk_selected` does on the way down has to be applied to the path itself:
    the default skip list, the ignore spec and the .gitignore files all speak
    about directories, and without this a project would be re-indexed for
    every write under .git. A watcher passes its own `gitignores` to keep what
    it read; without one, the tree's files are read for this path alone.
    """
    if gitignores is None and tree is not None:
        gitignores = GitIgnores(tree)
    if not selects_dir(posixpath.dirname(rel_path), ignore_spec, gitignores):
        return False
    if gitignores is not None and gitignores.ignores(rel_path):
        return False
    return selects_file(rel_path, ignore_spec, tree)


def selects_dir(
    rel_dir: str,
    ignore_spec: pathspec.PathSpec | None,
    gitignores: GitIgnores | None = None,
) -> bool:
    """Whether the walk would go down into a directory, every ancestor asked."""
    prefix = ""
    for part in rel_dir.split("/") if rel_dir else []:
        if part in DEFAULT_IGNORED_DIRS:
            return False
        prefix = posixpath.join(prefix, part)
        if ignored(f"{prefix}/", ignore_spec):
            return False
        if gitignores is not None and gitignores.ignores(prefix, is_dir=True):
            return False
    return True
