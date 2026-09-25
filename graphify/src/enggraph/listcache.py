"""The counts the projects listing shows, held in a diskcache.

Every finished index run rewrites its project's entry, so the dashboard reads
three numbers per project instead of counting the whole graph on each load.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

import diskcache
from psycopg2.extensions import cursor as Cursor

from enggraph.config import LIST_CACHE_DIR
from enggraph.storage import get_db_connection

LOG = logging.getLogger(__name__)

COUNTS_SQL = """
    SELECT (SELECT count(*) FROM graph_nodes WHERE project = %(project)s::text),
           (SELECT count(*) FROM graph_edges WHERE project = %(project)s::text),
           (SELECT count(*) FROM graph_nodes
             WHERE project = %(project)s::text AND type = 'file');
"""

PROJECTS_SQL = "SELECT name FROM projects WHERE indexed_at IS NOT NULL;"

_cache: diskcache.Cache | None = None
_cache_lock = threading.Lock()


def cache() -> diskcache.Cache:
    """Open the cache on first use, so importing this never touches the disk."""
    global _cache  # noqa: PLW0603 - one cache per process, by design
    with _cache_lock:
        if _cache is None:
            _cache = diskcache.Cache(LIST_CACHE_DIR)
        return _cache


def use(replacement: diskcache.Cache | None) -> None:
    """Point the module at another cache, or back at the default one."""
    global _cache  # noqa: PLW0603 - the tests swap it
    with _cache_lock:
        _cache = replacement


def count(cursor: Cursor, project: str) -> dict[str, Any]:
    """Count one project the way the listing shows it."""
    cursor.execute(COUNTS_SQL, {"project": project})
    nodes, edges, files = cursor.fetchone()
    return {
        "project": project,
        "nodes": int(nodes),
        "edges": int(edges),
        "files": int(files),
        "refreshed_at": datetime.now(UTC).isoformat(),
    }


def refresh(cursor: Cursor, project: str) -> dict[str, Any]:
    """Recount one project and store the answer."""
    entry = count(cursor, project)
    cache().set(project, entry)
    return entry


def read_all() -> list[dict[str, Any]]:
    """Return every stored entry, in no particular order."""
    store = cache()
    entries = (store.get(key) for key in store.iterkeys())
    return [entry for entry in entries if isinstance(entry, dict)]


def rename(old: str, new: str) -> None:
    """Move an entry to the name its project goes by now."""
    store = cache()
    entry = store.pop(old, default=None)
    if isinstance(entry, dict):
        store.set(new, {**entry, "project": new})


def forget(project: str) -> None:
    """Drop an entry whose project is gone."""
    cache().delete(project)


def warm() -> int:
    """Fill in every indexed project the cache has no entry for yet."""
    conn = get_db_connection()
    filled = 0
    try:
        with conn.cursor() as cursor:
            cursor.execute(PROJECTS_SQL)
            names = [str(row[0]) for row in cursor.fetchall()]
            store = cache()
            for name in names:
                if name not in store:
                    refresh(cursor, name)
                    filled += 1
        conn.commit()
    finally:
        conn.close()
    return filled


def warm_in_background() -> None:
    """Warm the cache on a thread, so a fresh volume does not delay start up."""

    def work() -> None:
        try:
            filled = warm()
        except Exception:  # noqa: BLE001 - the listing counts live meanwhile
            LOG.exception("Could not warm the listing cache")
            return
        if filled:
            LOG.info("Listing cache: counted %d project(s) it had no entry for", filled)

    threading.Thread(target=work, name="listcache-warm", daemon=True).start()
