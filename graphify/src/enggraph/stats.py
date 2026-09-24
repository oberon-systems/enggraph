"""The coverage gauges the dashboard reads, recomputed on a timer.

Counting how much of a project is embedded and described walks every node of
it. Doing that per request, per project, every few seconds is what kept the
database busy and the queues page from loading; here it is one pass over all
projects every STATS_REFRESH_SECONDS, stored in Valkey, and a request reads
the stored numbers.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from enggraph import embedjobs, queue
from enggraph.config import EMBED_MODEL, STATS_REFRESH_SECONDS
from enggraph.storage import embedding_coverage, get_db_connection, summary_coverage

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


def refresh() -> int:
    """Recompute every project's gauges and store them. Returns the project count."""
    done = {
        project: embedjobs.done_paths(project) for project in queue.projects_with_keys()
    }
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            embedded = embedding_coverage(cursor, EMBED_MODEL, done)
            described = summary_coverage(cursor)
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
    """Refresh the gauges now, then every STATS_REFRESH_SECONDS."""

    def __init__(self, every: int = STATS_REFRESH_SECONDS) -> None:
        """Take the refresh interval."""
        self._every = max(5, every)
        self._stop = threading.Event()

    def start(self) -> None:
        """Run the loop on a thread of its own."""
        threading.Thread(target=self.run, name="stats-loop", daemon=True).start()

    def stop(self) -> None:
        """Ask the loop to finish."""
        self._stop.set()

    def run(self) -> None:
        """Refresh until stopped. A failed pass keeps the previous numbers."""
        while True:
            try:
                refresh()
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                LOG.exception("Refreshing the coverage gauges failed")
            if self._stop.wait(self._every):
                return
