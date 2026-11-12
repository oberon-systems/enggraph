"""The one seam onto an indexed tree.

Listing a tree, reading a file of it, searching its text and hearing that it
changed all go through `Tree`. What answers is the host agent a project is
bound to, and nothing else ever reads a tree.
"""

from __future__ import annotations

import io
import json
import logging
import queue
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
    WORKER_API_TOKEN,
    WORKER_API_URL,
)

LOG = logging.getLogger(__name__)
# What the files of one fetch may add up to once unpacked.
MAX_FETCH_BYTES = 2 * 1024**3

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
                raise TreeRefused(str(detail.get("code", ""))) from error
            raise TreeUnavailable(f"the worker API answered {error.code}") from error
        except (OSError, ValueError) as error:
            LOG.error("The worker API did not answer %s: %s", op, error)
            raise TreeUnavailable("the worker API does not answer") from error

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


def decode(data: bytes) -> str:
    """Make text of a file's bytes: invalid UTF-8 dropped, line endings unified."""
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

    def _asked(self) -> Source:
        """Return the export to ask, or refuse one an operator has not approved."""
        if not self.source.approved:
            raise TreeUnavailable(f"{self.where} is not approved")
        return self.source

    def _call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _transport.call(self._asked(), op, args)
        except TreeRefused as refused:
            raise TreeUnavailable(f"{self.where}: {refused}") from refused

    def _data(self, op: str, args: dict[str, Any]) -> bytes:
        """Ask for bytes; a refusal about the file stays one, the rest is the tree."""
        try:
            return _transport.data(self._asked(), op, args)
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
            matches = _transport.call(self._asked(), "grep", args)["matches"]
        except TreeRefused as refused:
            # A bad pattern finds nothing.
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
                # Weighed before anything is written: only what was asked for,
                # as regular files, and no more of it than a fetch may hold.
                members = unpacking.getmembers()
                asked = set(rel_paths)
                strange = [one.name for one in members if one.name not in asked]
                size = sum(one.size for one in members)
                plain = all(one.isfile() for one in members)
                if strange or not plain or size > MAX_FETCH_BYTES:
                    raise TreeUnavailable(f"{self.where} answered a fetch with more")
                unpacking.extractall(directory, members=members, filter="data")
            yield directory


class Unserved:
    """A project no agent is bound to: never available, and every read raises."""

    def __init__(self, project: str) -> None:
        """Take the project nothing serves."""
        self.project = project
        self.where = "no host agent"

    def _refused(self) -> TreeUnavailable:
        return TreeUnavailable(unserved(self.project))

    def available(self) -> bool:
        """Say no: there is nothing to ask."""
        return False

    def walk(
        self, top: str = "", keep_dir: Keep = _all, keep_file: Keep = _all
    ) -> Iterator[str]:
        """Refuse."""
        raise self._refused()

    def is_file(self, rel_path: str) -> bool:
        """Refuse."""
        raise self._refused()

    def is_dir(self, rel_path: str) -> bool:
        """Refuse."""
        raise self._refused()

    def contains(self, rel_path: str) -> bool:
        """Refuse."""
        raise self._refused()

    def size(self, rel_path: str) -> int | None:
        """Refuse."""
        raise self._refused()

    def head(self, rel_path: str, limit: int) -> bytes:
        """Refuse."""
        raise self._refused()

    def read(self, rel_path: str) -> tuple[str | None, str]:
        """Refuse."""
        raise self._refused()

    def grep(
        self,
        pattern: str,
        ignore_lines: list[str],
        path_glob: str | None,
        limit: int,
        fixed: bool,
    ) -> list[tuple[str, int, str]]:
        """Refuse."""
        raise self._refused()

    def local(self, rel_paths: list[str]) -> AbstractContextManager[str]:
        """Refuse."""
        raise self._refused()


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
    bound project does not read as unbound for the length of a hiccup.
    """
    global _sources, _sources_read
    with _sources_lock:
        if time.monotonic() - _sources_read >= SOURCES_SECONDS:
            read = _read_sources()
            _sources_read = time.monotonic()
            if read is not None:
                _sources = read
        return _sources.get(project)


def of(project: str) -> Tree:
    """Return the tree of an indexed project: its agent when bound.

    An unbound one answers as unavailable, so nothing is run or pruned.
    """
    found = source(project)
    if found is not None:
        return RemoteTree(found)
    return Unserved(project)


def unserved(project: str) -> str:
    """Say why a project's tree cannot be read, for whoever asked."""
    return (
        f"{project} cannot be read: no host agent is bound to it and connected. "
        "Bind it on the Agents page, or start its agent."
    )


def changes(
    projects: Iterable[str], stop: threading.Event
) -> Iterator[tuple[str, str]]:
    """Yield (project, relative path) for every change under the named trees.

    A project is heard over its agent's socket, and one nothing serves is not
    heard at all. `RESYNC` in place of a path asks for a full pass. Raises
    OSError where the sockets are not held.
    """
    served: dict[tuple[str, str], str] = {}
    for project in projects:
        tree = of(project)
        if isinstance(tree, RemoteTree) and tree.source.approved:
            served[(tree.source.agent_id, tree.source.export)] = project
    if not served:
        return

    heard: queue.Queue[tuple[str, str]] = queue.Queue()

    def listen(agent_id: str, export: str, paths: list[str] | None) -> None:
        project = served.get((agent_id, export))
        if project is not None:
            for path in [RESYNC] if paths is None else paths:
                heard.put((project, path))

    unsubscribe = _transport.subscribe(listen)
    try:
        while not stop.is_set():
            try:
                change = heard.get(timeout=1)
            except queue.Empty:
                continue
            yield change
    finally:
        unsubscribe()
