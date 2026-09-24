"""The Valkey the queues live in: one client per process, and the key names.

Memory only, with LRU eviction: any key here may be gone on the next read. The
graph is what says which work is owed, so a lost queue costs one sweep, never
a result - results are written to Postgres.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import valkey

from enggraph.config import VALKEY_URL

PREFIX = "eg"

_client: valkey.Valkey | None = None
_lock = threading.Lock()


def client() -> valkey.Valkey:
    """Return the process-wide client, connecting on first use."""
    global _client
    with _lock:
        if _client is None:
            _client = valkey.Valkey.from_url(VALKEY_URL, decode_responses=True)
        return _client


def use(replacement: valkey.Valkey | None) -> None:
    """Swap the client, for the suite's in-process server."""
    global _client
    with _lock:
        _client = replacement


def now() -> float:
    """Wall-clock seconds, the unit every lease and deadline is kept in."""
    return time.time()


def key(*parts: object) -> str:
    """Join a key under the common prefix."""
    return ":".join((PREFIX, *(str(part) for part in parts)))


def scan(pattern: str) -> Iterator[str]:
    """Yield every key matching a pattern, without blocking the server."""
    yield from client().scan_iter(match=pattern, count=500)


def forget_project(project: str) -> int:
    """Drop every queue key of one project. Returns how many went."""
    names = [
        *scan(key("embed", project, "*")),
        *scan(key("stats", "*", project)),
        *scan(key("cov", project)),
        key("sum", "opened", project),
        key("index", "running", project),
    ]
    running = client().hget(key("sum", "running"), project)
    if running:
        names.extend(scan(key("sum", "job", running) + "*"))
        client().hdel(key("sum", "running"), project)
    return int(client().delete(*names)) if names else 0


def projects_with_keys() -> set[str]:
    """Return the projects any embedding key or gauge is kept for."""
    found: set[str] = set()
    for name in scan(key("embed", "*")):
        parts = name.split(":")
        if len(parts) >= 4:
            found.add(parts[2])
    return found
