"""The one seam onto an indexed tree.

Listing a tree, reading a file of it, searching its text and hearing that it
changed all go through `Tree`. What answers today is the read-only mount at
`CODE_ROOT/<project>`; nothing outside this module may assume that, because
an agent serving the tree remotely is what replaces the mounts.
"""

from __future__ import annotations

import logging
import os
import posixpath
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from enggraph.core.config import CODE_ROOT, MAX_FILE_BYTES, SCAN_PATH

LOG = logging.getLogger(__name__)

RIPGREP = "rg"
GREP_TIMEOUT_SECONDS = 60

UNREADABLE = "unreadable"
OVERSIZED = "over the size limit"

Keep = Callable[[str], bool]


def _all(_: str) -> bool:
    return True


class Tree(Protocol):
    """One project's tree, addressed by paths relative to its root."""

    project: str
    where: str

    def available(self) -> bool:
        """Say whether the tree can be read at all."""

    def walk(
        self, top: str = "", keep_dir: Keep = _all, keep_file: Keep = _all
    ) -> Iterator[str]:
        """Yield the files under `top`, sorted, never through a linked directory."""

    def is_file(self, rel_path: str) -> bool:
        """Say whether a path names a file."""

    def is_dir(self, rel_path: str) -> bool:
        """Say whether a path names a directory."""

    def contains(self, rel_path: str) -> bool:
        """Say whether a path, links resolved, stays inside the tree."""

    def size(self, rel_path: str) -> int | None:
        """Return a file's size in bytes, or None when it cannot be asked."""

    def head(self, rel_path: str, limit: int) -> bytes:
        """Return the first bytes of a file, empty when it cannot be read."""

    def read(self, rel_path: str) -> tuple[str | None, str]:
        """Return a file's text, or None and the reason there is none."""

    def grep(
        self,
        pattern: str,
        ignore_lines: list[str],
        path_glob: str | None,
        limit: int,
        fixed: bool,
    ) -> list[tuple[str, int, str]]:
        """Return up to `limit` (path, line number, line) matches of a regex."""

    def local(self, rel_paths: list[str]) -> AbstractContextManager[str]:
        """Give a directory holding the named files at their relative paths."""


class MountTree:
    """A tree read from a directory of this container."""

    def __init__(self, project: str, root: str) -> None:
        """Take the project and the directory its tree is found in."""
        self.project = project
        self.where = root

    def _full(self, rel_path: str) -> str:
        return os.path.join(self.where, rel_path)

    def available(self) -> bool:
        """Say whether the mount is a live directory.

        A bind mount outlives a host directory deleted and recreated behind it;
        what it still shows is the old inode, empty and with no links left.
        """
        try:
            found = os.stat(self.where)
        except OSError:
            return False
        return stat.S_ISDIR(found.st_mode) and found.st_nlink > 0

    def walk(
        self, top: str = "", keep_dir: Keep = _all, keep_file: Keep = _all
    ) -> Iterator[str]:
        """Yield the files under `top`, sorted, never through a linked directory."""
        start = self._full(top) if top else self.where
        for current_dir, dir_names, file_names in os.walk(start):
            rel_dir = os.path.relpath(current_dir, self.where)
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
        """Say whether a path, links resolved, stays inside the tree.

        A walk never descends a linked directory, but a linked file is
        indexed and can point anywhere, so links are resolved before comparing.
        """
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
        """Return a file's text, or None and the reason there is none.

        The reason travels with the content because a skipped file is a file
        the graph will not describe, and a run reports that at its end.
        """
        full_path = self._full(rel_path)
        try:
            size = os.path.getsize(full_path)
        except OSError:
            LOG.exception("Failed to stat %s", rel_path)
            return None, UNREADABLE
        if size > MAX_FILE_BYTES:
            LOG.info("Skipping %s: %d bytes exceeds the limit", rel_path, size)
            return None, OVERSIZED
        try:
            with open(full_path, encoding="utf-8", errors="ignore") as handle:
                return handle.read(), ""
        except OSError:
            LOG.exception("Failed to read %s", rel_path)
            return None, UNREADABLE

    def grep(
        self,
        pattern: str,
        ignore_lines: list[str],
        path_glob: str | None,
        limit: int,
        fixed: bool,
    ) -> list[tuple[str, int, str]]:
        """Return up to `limit` (path, line number, line) matches of a regex."""
        handle, ignore = tempfile.mkstemp(prefix="enggraph-grep-", suffix=".ignore")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            for line in ignore_lines:
                out.write(f"{line}\n")
        command = [
            RIPGREP,
            "--no-ignore",
            "--hidden",
            "--no-heading",
            "--with-filename",
            "--null",
            "--line-number",
            "--color=never",
            "--ignore-case",
            "--ignore-file",
            ignore,
            "--max-count",
            str(limit),
        ]
        if fixed:
            command.append("--fixed-strings")
        if path_glob:
            command += ["--glob", path_glob]
        command += ["--regexp", pattern, "."]
        found: list[tuple[str, int, str]] = []
        try:
            with subprocess.Popen(
                command,
                cwd=self.where,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
            ) as process:
                # A search over a large tree is cut off rather than left running.
                timer = threading.Timer(GREP_TIMEOUT_SECONDS, process.kill)
                timer.start()
                try:
                    for raw in process.stdout or ():
                        parsed = _parse(raw.rstrip("\n"))
                        if parsed is None:
                            continue
                        found.append(parsed)
                        if len(found) >= limit:
                            break
                finally:
                    timer.cancel()
                    process.kill()
                    process.wait()
        finally:
            os.unlink(ignore)
        return found

    @contextmanager
    def local(self, rel_paths: list[str]) -> Iterator[str]:
        """Give a directory holding the named files at their relative paths."""
        yield self.where


def _parse(line: str) -> tuple[str, int, str] | None:
    path, separator, rest = line.partition("\0")
    number, separator2, text = rest.partition(":")
    if not separator or not separator2 or not number.isdigit():
        return None
    return path.removeprefix("./"), int(number), text


def of(project: str) -> Tree:
    """Return the tree of an indexed project.

    The compose override is generated from the same name, so a mount and the
    row it belongs to cannot disagree about what a project is called.
    """
    return MountTree(project, posixpath.join(CODE_ROOT, project))


def at(path: str) -> Tree:
    """Return the tree found in a directory, whatever project it is."""
    return MountTree("", path)


def pending() -> Tree:
    """Return the tree being onboarded, which is no project yet."""
    return at(SCAN_PATH)


def changes(
    projects: Iterable[str], stop: threading.Event
) -> Iterator[tuple[str, str]]:
    """Yield (project, relative path) for every change under the named trees.

    One watch for all of them: the watches are a kernel resource. Raises
    OSError when the watch cannot be built or kept, a limit included.
    """
    # Imported here so a host where the watch cannot be built still starts.
    from watchfiles import watch

    roots = {of(project).where: project for project in projects}
    # Longest first, so a tree mounted inside another is matched against itself.
    order = sorted(roots, key=len, reverse=True)
    # A tree holds directories this process cannot read, and one of them
    # would otherwise refuse the watch for every project at once.
    for batch in watch(*order, stop_event=stop, ignore_permission_denied=True):
        for _, path in batch:
            for root in order:
                if path == root or path.startswith(f"{root}{os.sep}"):
                    rel_path = os.path.relpath(path, root).replace(os.sep, "/")
                    yield roots[root], rel_path
                    break
