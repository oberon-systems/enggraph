"""The one seam onto an indexed tree.

Listing a tree, reading a file of it, searching its text and hearing that it
changed all go through `Tree`. What answers is the read-only mount at
`CODE_ROOT/<project>`, or the host agent a project is bound to; nothing outside
this module may assume either.
"""

from __future__ import annotations

import io
import json
import logging
import os
import posixpath
import queue
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg2

from enggraph.core import db
from enggraph.core.config import (
    CODE_ROOT,
    MAX_FILE_BYTES,
    SCAN_PATH,
    WORKER_API_TOKEN,
    WORKER_API_URL,
)

LOG = logging.getLogger(__name__)

RIPGREP = "rg"
GREP_TIMEOUT_SECONDS = 60

UNREADABLE = "unreadable"
OVERSIZED = "over the size limit"
DENIED = "denied"
# Not a path: what `changes` yields when a whole tree has to be looked at again.
RESYNC = ""

BAD_ARGS = "bad_args"
# About the tree or the agent, not about a file: nothing can be said of the file.
TREE_REFUSALS = frozenset({"busy", BAD_ARGS, "unknown_op", "unknown_export"})
SOURCES_SECONDS = 5
HTTP_SECONDS = 330
SOURCES_SQL = """
SELECT s.project, s.agent_id, s.export, s.host_path,
       COALESCE(a.state = 'enabled' AND e.state = 'approved', FALSE) AS approved
  FROM project_sources AS s
  LEFT JOIN agents AS a ON a.id = s.agent_id
  LEFT JOIN agent_exports AS e ON e.agent_id = s.agent_id AND e.name = s.export
"""

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


class TreeUnavailable(OSError):
    """A tree that cannot answer now, which says nothing about its files."""


class TreeRefused(Exception):
    """A tree that answered, and said no."""

    def __init__(self, code: str, message: str = "") -> None:
        """Keep the code the agent answered with."""
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class Source:
    """The export a project is bound to."""

    project: str
    agent_id: str
    export: str
    host_path: str
    approved: bool


Listener = Callable[[str, str, list[str] | None], None]


class Transport(Protocol):
    """How a `RemoteTree` reaches the agent: the hub in-process, HTTP elsewhere."""

    def connected(self, source: Source) -> bool:
        """Say whether the export can be asked now."""

    def call(self, source: Source, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run an operation answered with JSON, its batches put together."""

    def data(self, source: Source, op: str, args: dict[str, Any]) -> bytes:
        """Run an operation answered with bytes."""

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Hear (agent id, export, paths or None); return what stops hearing."""


class HttpTransport:
    """Reach the worker API, which holds the sockets, from another container."""

    def _post(self, source: Source, op: str, args: dict[str, Any]) -> bytes:
        if not WORKER_API_URL:
            raise TreeUnavailable("WORKER_API_URL is not set")
        request = urllib.request.Request(
            f"{WORKER_API_URL}/internal/trees/{source.project}/{op}",
            data=json.dumps(args).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {WORKER_API_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_SECONDS) as answer:
                return bytes(answer.read())
        except urllib.error.HTTPError as error:
            if error.code == 422:
                detail = json.loads(error.read()).get("detail", {})
                raise TreeRefused(
                    str(detail.get("code", "")), str(detail.get("message", ""))
                ) from error
            raise TreeUnavailable(f"the worker API answered {error.code}") from error
        except (OSError, ValueError) as error:
            raise TreeUnavailable(f"the worker API: {error}") from error

    def connected(self, source: Source) -> bool:
        """Ask the service that holds the socket."""
        try:
            answer = json.loads(self._post(source, "available", {}))
        except (TreeUnavailable, TreeRefused, ValueError):
            return False
        return answer.get("available") is True

    def call(self, source: Source, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run an operation answered with JSON."""
        answer: dict[str, Any] = json.loads(self._post(source, op, args))
        return answer

    def data(self, source: Source, op: str, args: dict[str, Any]) -> bytes:
        """Run an operation answered with bytes."""
        return self._post(source, op, args)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Refuse: only the worker API holds the sockets, and only it schedules."""
        raise TreeUnavailable("changes are heard where the sockets are held")


_transport: Transport = HttpTransport()


def use_transport(transport: Transport) -> None:
    """Have this process reach agents directly: it is the one holding their sockets."""
    global _transport
    _transport = transport


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


def decode(data: bytes) -> str:
    """Make of a file's bytes the text `MountTree.read` makes of the same file."""
    with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="ignore") as text:
        return text.read()


class RemoteTree:
    """A tree its agent serves. A failure raises: it never reads as an empty tree."""

    def __init__(self, source: Source) -> None:
        """Take the export the project is bound to."""
        self.source = source
        self.project = source.project
        self.where = f"export {source.export} of agent {source.agent_id}"

    def _args(self, **args: object) -> dict[str, Any]:
        return {"export": self.source.export, **args}

    def _call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _transport.call(self.source, op, args)
        except TreeRefused as refused:
            raise TreeUnavailable(f"{self.where}: {refused}") from refused

    def _data(self, op: str, args: dict[str, Any]) -> bytes:
        """Ask for bytes; a refusal about the file stays one, the rest is the tree."""
        try:
            return _transport.data(self.source, op, args)
        except TreeRefused as refused:
            if refused.code in TREE_REFUSALS:
                raise TreeUnavailable(f"{self.where}: {refused}") from refused
            raise

    def available(self) -> bool:
        """Say whether the export is approved and its agent is connected."""
        return self.source.approved and _transport.connected(self.source)

    def listing(self, top: str = "", md5: bool = False) -> dict[str, Any]:
        """Return one directory: its files, hashed if asked, and its directories."""
        return self._call("walk", self._args(top=top, deep=False, md5=md5))

    def walk(
        self, top: str = "", keep_dir: Keep = _all, keep_file: Keep = _all
    ) -> Iterator[str]:
        """Yield the files under `top`, sorted, never through a linked directory.

        One directory per request, so what `keep_dir` prunes is never read at
        all. A root that answers nothing raises: an agent's bad moment must not
        read as a tree whose files were all deleted, which would prune a graph.
        """
        pending = [top]
        while pending:
            current = pending.pop()
            listing = self.listing(current)
            empty = not listing["entries"] and not listing["dirs"]
            if empty and not top and not current:
                raise TreeUnavailable(f"{self.where} answered with nothing")
            for skipped in listing.get("skipped", []):
                LOG.warning(
                    "%s of %s not read: %s",
                    skipped["path"],
                    self.project,
                    skipped["reason"],
                )
            for entry in listing["entries"]:
                if keep_file(entry["path"]):
                    yield entry["path"]
            kept = [one for one in listing["dirs"] if keep_dir(one)]
            pending.extend(reversed(kept))

    def _stat(self, rel_path: str) -> dict[str, Any]:
        answer: dict[str, Any] = self._call("stat", self._args(paths=[rel_path]))
        stats: dict[str, Any] = answer["stats"][0]
        return stats

    def is_file(self, rel_path: str) -> bool:
        """Say whether a path names a file."""
        return bool(self._stat(rel_path)["is_file"])

    def is_dir(self, rel_path: str) -> bool:
        """Say whether a path names a directory."""
        return bool(self._stat(rel_path)["is_dir"])

    def contains(self, rel_path: str) -> bool:
        """Say whether a path, links resolved, stays inside the tree."""
        return bool(self._stat(rel_path)["contained"])

    def size(self, rel_path: str) -> int | None:
        """Return a file's size in bytes, or None when it cannot be asked."""
        size = self._stat(rel_path)["size"]
        return None if size is None else int(size)

    def head(self, rel_path: str, limit: int) -> bytes:
        """Return the first bytes of a file, empty when it cannot be read."""
        try:
            return self._data("head", self._args(path=rel_path, n=limit))
        except TreeRefused:
            return b""

    def read(self, rel_path: str) -> tuple[str | None, str]:
        """Return a file's text, or None and the reason there is none."""
        try:
            return decode(self._data("read", self._args(path=rel_path))), ""
        except TreeRefused as refused:
            known = refused.code in (OVERSIZED, DENIED)
            return None, refused.code if known else UNREADABLE

    def grep(
        self,
        pattern: str,
        ignore_lines: list[str],
        path_glob: str | None,
        limit: int,
        fixed: bool,
    ) -> list[tuple[str, int, str]]:
        """Return up to `limit` (path, line number, line) matches of a regex."""
        args = self._args(
            pattern=pattern,
            ignore_lines=ignore_lines,
            path_glob=path_glob,
            limit=limit,
            fixed=fixed,
        )
        try:
            matches = _transport.call(self.source, "grep", args)["matches"]
        except TreeRefused as refused:
            # A bad pattern finds nothing, as ripgrep exiting on it does on a mount.
            if refused.code == BAD_ARGS:
                return []
            raise TreeUnavailable(f"{self.where}: {refused}") from refused
        return [(one["path"], int(one["line"]), one["text"]) for one in matches][:limit]

    @contextmanager
    def local(self, rel_paths: list[str]) -> Iterator[str]:
        """Give a directory holding the named files at their relative paths."""
        try:
            archive = self._data("fetch", self._args(paths=rel_paths))
        except TreeRefused as refused:
            raise TreeUnavailable(f"{self.where}: {refused}") from refused
        with tempfile.TemporaryDirectory(prefix="enggraph-fetch-") as directory:
            with tarfile.open(fileobj=io.BytesIO(archive)) as unpacking:
                unpacking.extractall(directory, filter="data")
            yield directory


def _parse(line: str) -> tuple[str, int, str] | None:
    path, separator, rest = line.partition("\0")
    number, separator2, text = rest.partition(":")
    if not separator or not separator2 or not number.isdigit():
        return None
    return path.removeprefix("./"), int(number), text


_sources_lock = threading.Lock()
_sources: dict[str, Source] = {}
_sources_read = 0.0


def _read_sources() -> dict[str, Source] | None:
    """Read every binding, or None when the database cannot say."""
    try:
        rows = db.query(SOURCES_SQL)
    except (psycopg2.Error, RuntimeError) as error:
        LOG.debug("The project sources were not read: %s", error)
        return None
    return {
        row["project"]: Source(
            project=row["project"],
            agent_id=row["agent_id"],
            export=row["export"],
            host_path=row["host_path"],
            approved=bool(row["approved"]),
        )
        for row in rows
    }


def source(project: str) -> Source | None:
    """Return the binding of a project, read at most once every few seconds.

    A database that does not answer leaves the last answer standing, so a
    bound project does not fall back to a mount for the length of a hiccup.
    """
    global _sources, _sources_read
    with _sources_lock:
        if time.monotonic() - _sources_read >= SOURCES_SECONDS:
            read = _read_sources()
            _sources_read = time.monotonic()
            if read is not None:
                _sources = read
        return _sources.get(project)


def mounted(project: str) -> MountTree:
    """Return the mount of a project.

    The compose override is generated from the same name, so a mount and the
    row it belongs to cannot disagree about what a project is called.
    """
    return MountTree(project, posixpath.join(CODE_ROOT, project))


def of(project: str) -> Tree:
    """Return the tree of an indexed project: its agent when bound, or its mount."""
    found = source(project)
    if found is not None:
        return RemoteTree(found)
    return mounted(project)


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

    A project its agent serves is heard over the socket, the rest through one
    watch on the mounts. `RESYNC` in place of a path asks for a full pass. Raises
    OSError when the watch cannot be built or kept, a limit included.
    """
    served: dict[tuple[str, str], str] = {}
    mounts: list[str] = []
    for project in projects:
        tree = of(project)
        if isinstance(tree, RemoteTree):
            served[(tree.source.agent_id, tree.source.export)] = project
        else:
            mounts.append(project)
    if not served:
        yield from _mount_changes(mounts, stop)
        return

    heard: queue.Queue[tuple[str, str] | Exception] = queue.Queue()

    def listen(agent_id: str, export: str, paths: list[str] | None) -> None:
        project = served.get((agent_id, export))
        if project is not None:
            for path in [RESYNC] if paths is None else paths:
                heard.put((project, path))

    def watch_mounts() -> None:
        try:
            for change in _mount_changes(mounts, stop):
                heard.put(change)
        except Exception as error:  # noqa: BLE001 - raised where it is read
            heard.put(error)

    unsubscribe = _transport.subscribe(listen)
    try:
        if mounts:
            threading.Thread(target=watch_mounts, daemon=True).start()
        while not stop.is_set():
            try:
                change = heard.get(timeout=1)
            except queue.Empty:
                continue
            if isinstance(change, Exception):
                raise change
            yield change
    finally:
        unsubscribe()


def _mount_changes(
    projects: Iterable[str], stop: threading.Event
) -> Iterator[tuple[str, str]]:
    """Watch the mounts: one watch for all, since watches are a kernel resource."""
    # Imported here so a host where the watch cannot be built still starts.
    from watchfiles import watch

    roots = {mounted(project).where: project for project in projects}
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
