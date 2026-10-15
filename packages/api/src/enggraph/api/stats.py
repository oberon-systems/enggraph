"""The coverage gauges the dashboard reads, recomputed on a timer.

Counting how much of a project is embedded and described walks every node of
it. Doing that per request kept the database busy and the queues page from
loading, and so did one pass over all projects every minute: the graph was
read from disk all day. The writers of what is counted ask for a recount of
their project, the loop recounts those every STATS_REFRESH_SECONDS and every
project every STATS_FULL_SECONDS, and a request reads the numbers from Valkey.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from enggraph.core import embedjobs, queue
from enggraph.core.config import (
    EMBED_MODEL,
    STATS_FULL_SECONDS,
    STATS_REFRESH_SECONDS,
)
from enggraph.core.storage import (
    embedding_coverage,
    get_db_connection,
    summary_coverage,
)

LOG = logging.getLogger(__name__)

EMPTY_EMBEDDING = {
    "chunks": 0,
    "summary_chunks": 0,
    "summaries": 0,
    "summaries_embedded": 0,
    "files": 0,
    "indexed_files": 0,
    "skipped": 0,
}
EMPTY_SUMMARY: dict[str, Any] = {
    "files": 0,
    "described": 0,
    "manual": 0,
    "skipped": 0,
    "levels": {
        name: {"total": 0, "described": 0, "manual": 0, "skipped": 0}
        for name in ("directories", "entities")
    },
}


def refresh(projects: set[str] | None = None) -> int:
    """Recompute the gauges of `projects`, or of all, and store them.

    Returns how many projects were counted.
    """
    if projects is not None and not projects:
        queue.client().set(queue.key("cov", "at"), queue.now())
        return 0
    among = sorted(projects) if projects is not None else None
    done = {
        project: embedjobs.done_paths(project)
        for project in (among or queue.projects_with_keys())
    }
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            embedded = embedding_coverage(cursor, EMBED_MODEL, done, among)
            described = summary_coverage(cursor, among)
        conn.commit()
    finally:
        conn.close()
    pipe = queue.client().pipeline()
    for project in set(embedded) | set(described):
        pipe.hset(
            queue.key("cov", project),
            mapping={
                "embedding": json.dumps(embedded.get(project, EMPTY_EMBEDDING)),
                "summary": json.dumps(described.get(project, EMPTY_SUMMARY)),
            },
        )
    pipe.set(queue.key("cov", "at"), queue.now())
    pipe.execute()
    return len(set(embedded) | set(described))


def read(project: str, part: str) -> dict[str, Any]:
    """Return one project's stored gauge, or zeros until the first pass."""
    raw = queue.client().hget(queue.key("cov", project), part)
    if raw:
        return dict(json.loads(raw))
    empty = EMPTY_EMBEDDING if part == "embedding" else EMPTY_SUMMARY
    return json.loads(json.dumps(empty))


def refreshed_at() -> float | None:
    """When the gauges were last written, in epoch seconds."""
    raw = queue.client().get(queue.key("cov", "at"))
    return float(raw) if raw else None


class StatsLoop:
    """Count every project now, then what changed every STATS_REFRESH_SECONDS."""

    def __init__(
        self, every: int = STATS_REFRESH_SECONDS, full: int = STATS_FULL_SECONDS
    ) -> None:
        """Take the refresh intervals."""
        self._every = max(5, every)
        self._full = max(self._every, full)
        self._full_at = 0.0
        self._again: set[str] = set()
        self._stop = threading.Event()

    def start(self) -> None:
        """Run the loop on a thread of its own."""
        threading.Thread(target=self.run, name="stats-loop", daemon=True).start()

    def stop(self) -> None:
        """Ask the loop to finish."""
        self._stop.set()

    def tick(self) -> int:
        """Recount the projects asked for, or all of them when a full pass is due.

        A project is counted once more on the next tick: a write that asked for
        it may commit after this count read the graph.
        """
        due, everything = queue.owed_recounts()
        settle = self._again
        self._again = due | settle
        if everything or time.monotonic() >= self._full_at:
            counted = refresh()
            self._full_at = time.monotonic() + self._full
        else:
            counted = refresh(due | settle)
        self._again = due
        return counted

    def run(self) -> None:
        """Refresh until stopped. A failed pass keeps the previous numbers."""
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                LOG.exception("Refreshing the coverage gauges failed")
            if self._stop.wait(self._every):
                return
