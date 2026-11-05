"""A tree of a temporary directory, for tests: no stack reads a tree this way."""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pathspec

from enggraph.core import trees
from enggraph.core.config import MAX_FILE_BYTES


def _all(_: str) -> bool:
    return True


class DirTree:
    """Answer as an agent does, from a directory of the test's."""

    def __init__(self, project: str, root: object) -> None:
        """Take the project and the directory standing for its tree."""
        self.project = project
        self.where = str(root)

    def _full(self, rel_path: str) -> str:
        return os.path.join(self.where, rel_path)

    def available(self) -> bool:
        """Say whether the directory is there."""
        return os.path.isdir(self.where)

    def walk(
        self,
        top: str = "",
        keep_dir: Callable[[str], bool] = _all,
        keep_file: Callable[[str], bool] = _all,
    ) -> Iterator[str]:
        """Yield the files under `top`, sorted, never through a linked directory."""
        for current, dir_names, file_names in os.walk(self._full(top)):
            rel_dir = os.path.relpath(current, self.where)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            dir_names[:] = [
                name
                for name in sorted(dir_names)
                if keep_dir(posixpath.join(rel_dir, name))
            ]
            for file_name in sorted(file_names):
                rel_path = posixpath.join(rel_dir, file_name)
                if keep_file(rel_path):
                    yield rel_path

    def is_file(self, rel_path: str) -> bool:
        """Say whether a path names a file."""
        return os.path.isfile(self._full(rel_path))

    def is_dir(self, rel_path: str) -> bool:
        """Say whether a path names a directory."""
        return os.path.isdir(self._full(rel_path))

    def contains(self, rel_path: str) -> bool:
        """Say whether a path, links resolved, stays inside the tree."""
        root = os.path.realpath(self.where)
        full = os.path.realpath(os.path.join(root, rel_path))
        return full == root or full.startswith(root + os.sep)

    def size(self, rel_path: str) -> int | None:
        """Return a file's size in bytes, or None when it cannot be asked."""
        try:
            return os.path.getsize(self._full(rel_path))
        except OSError:
            return None

    def head(self, rel_path: str, limit: int) -> bytes:
        """Return the first bytes of a file, empty when it cannot be read."""
        try:
            with open(self._full(rel_path), "rb") as handle:
                return handle.read(limit)
        except OSError:
            return b""

    def read(self, rel_path: str) -> tuple[str | None, str]:
        """Return a file's text, or None and the reason there is none."""
        size = self.size(rel_path)
        if size is None:
            return None, trees.UNREADABLE
        if size > MAX_FILE_BYTES:
            return None, trees.OVERSIZED
        with open(self._full(rel_path), "rb") as handle:
            return trees.decode(handle.read()), ""

    def grep(
        self,
        pattern: str,
        ignore_lines: list[str],
        path_glob: str | None,
        limit: int,
        fixed: bool,
    ) -> list[tuple[str, int, str]]:
        """Return up to `limit` (path, line number, line) matches of a regex."""
        ignored = pathspec.PathSpec.from_lines("gitwildmatch", ignore_lines)
        wanted = re.compile(re.escape(pattern) if fixed else pattern, re.IGNORECASE)
        found: list[tuple[str, int, str]] = []
        for rel_path in self.walk():
            if ignored.match_file(rel_path):
                continue
            if path_glob and not fnmatch.fnmatch(rel_path, path_glob):
                continue
            text, _ = self.read(rel_path)
            for number, line in enumerate((text or "").splitlines(), 1):
                if wanted.search(line):
                    found.append((rel_path, number, line))
        return found[:limit]

    @contextmanager
    def local(self, rel_paths: list[str]) -> Iterator[str]:
        """Give a directory holding the named files at their relative paths."""
        yield self.where
