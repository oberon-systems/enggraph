"""The counts the projects listing shows, held in one Valkey hash.

Every finished index run rewrites its project's entry, so the dashboard reads
the counts and their breakdowns instead of counting the whole graph on each
load.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

from psycopg2.extensions import cursor as Cursor

from enggraph.core import queue
from enggraph.core.storage import get_db_connection

LOG = logging.getLogger(__name__)

COUNTS_SQL = """
    SELECT (SELECT count(*) FROM nodes WHERE project = %(project)s::text),
           (SELECT count(*) FROM edges WHERE project = %(project)s::text),
           (SELECT count(*) FROM nodes
             WHERE project = %(project)s::text AND type = 'file'),
           (SELECT COALESCE(json_object_agg(type, total), '{}')
              FROM (SELECT type, count(*) AS total FROM nodes
                     WHERE project = %(project)s::text GROUP BY type) AS t),
           (SELECT COALESCE(json_object_agg(relation_type, total), '{}')
              FROM (SELECT relation_type, count(*) AS total FROM edges
                     WHERE project = %(project)s::text
                     GROUP BY relation_type) AS r),
           (SELECT COALESCE(json_object_agg(extension, total), '{}')
              FROM (SELECT CASE
                             WHEN strpos(name, '.') = 0 THEN name
                             ELSE '.' || lower(split_part(name, '.', -1))
                           END AS extension,
                           count(*) AS total
                      FROM (SELECT split_part(id, '/', -1) AS name FROM nodes
                             WHERE project = %(project)s::text
                               AND type = 'file') AS f
                     GROUP BY extension) AS x),
           (SELECT count(*) FROM indexed_files
             WHERE project = %(project)s::text);
"""
# An entry written before the breakdowns were kept is counted again on start.
BREAKDOWNS = ("types", "relations", "extensions", "hashed_files")

PROJECTS_SQL = "SELECT name FROM projects WHERE indexed_at IS NOT NULL;"

KEY = queue.key("listing")

_warming = threading.Lock()


def _load(raw: str | None) -> dict[str, Any] | None:
    """Decode a stored entry, or nothing when it is missing or unreadable."""
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _store(project: str, entry: dict[str, Any]) -> None:
    """Write one project's entry."""
    queue.client().hset(KEY, project, json.dumps(entry))


def count(cursor: Cursor, project: str) -> dict[str, Any]:
    """Count one project the way the listing shows it."""
    cursor.execute(COUNTS_SQL, {"project": project})
    nodes, edges, files, types, relations, extensions, hashed = cursor.fetchone()
    return {
        "project": project,
        "nodes": int(nodes),
        "edges": int(edges),
        "files": int(files),
        "types": {str(key): int(value) for key, value in (types or {}).items()},
        "relations": {str(key): int(value) for key, value in (relations or {}).items()},
        "extensions": {
            str(key): int(value) for key, value in (extensions or {}).items()
        },
        "hashed_files": int(hashed),
        "refreshed_at": datetime.now(UTC).isoformat(),
    }


def refresh(cursor: Cursor, project: str) -> dict[str, Any]:
    """Recount one project and store the answer."""
    entry = count(cursor, project)
    _store(project, entry)
    return entry


def read_all() -> list[dict[str, Any]]:
    """Return every stored entry, in no particular order."""
    entries = (_load(raw) for raw in queue.client().hvals(KEY))
    return [entry for entry in entries if entry is not None]


def rename(old: str, new: str) -> None:
    """Move an entry to the name its project goes by now."""
    entry = _load(queue.client().hget(KEY, old))
    pipe = queue.client().pipeline()
    pipe.hdel(KEY, old)
    if entry is not None:
        pipe.hset(KEY, new, json.dumps({**entry, "project": new}))
    pipe.execute()


def forget(project: str) -> None:
    """Drop an entry whose project is gone."""
    queue.client().hdel(KEY, project)


def complete(entry: object) -> bool:
    """Say whether an entry carries everything the dashboard reads from it."""
    return isinstance(entry, dict) and all(key in entry for key in BREAKDOWNS)


def warm() -> int:
    """Fill in every indexed project the cache has no complete entry for."""
    conn = get_db_connection()
    filled = 0
    try:
        with conn.cursor() as cursor:
            cursor.execute(PROJECTS_SQL)
            names = [str(row[0]) for row in cursor.fetchall()]
            stored = queue.client().hgetall(KEY)
            for name in names:
                if not complete(_load(stored.get(name))):
                    refresh(cursor, name)
                    filled += 1
        conn.commit()
    finally:
        conn.close()
    return filled


def warm_in_background() -> None:
    """Fill in the missing entries on a thread, one warm at a time."""
    if not _warming.acquire(blocking=False):
        return

    def work() -> None:
        try:
            filled = warm()
        except Exception:  # noqa: BLE001 - the listing counts live meanwhile
            LOG.exception("Could not warm the listing cache")
            return
        finally:
            _warming.release()
        if filled:
            LOG.info("Listing cache: counted %d project(s) it had no entry for", filled)

    threading.Thread(target=work, name="listcache-warm", daemon=True).start()
