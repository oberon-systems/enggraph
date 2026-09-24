"""Run an index in this process, and keep a row saying how it went.

Indexing used to be a container the host started for one tree. The API holds
every tree at `/code/<project>` and carries the same code, so it does the work
itself; what a caller loses by not watching a log, it gets back from the row
this module writes.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

import valkey
from psycopg2.extensions import cursor as Cursor

from enggraph import embedjobs, features, queue
from enggraph.config import EMBED_MODEL, FEATURE_EMBEDDING, INDEX_LOCK_SECONDS
from enggraph.identifiers import is_mounted, project_mount
from enggraph.storage import get_db_connection

LOG = logging.getLogger(__name__)

COLUMNS = (
    "id, project, status, fresh, project_type, files, with_node, "
    "entities, edges, pruned, failures, gaps, error, started_at, finished_at"
)
# What scan_and_build_graph returns, in the order the row stores it.
COUNTS = ("files", "with_node", "entities", "edges", "pruned", "failures", "gaps")


def row_view(row: tuple[Any, ...]) -> dict[str, Any]:
    """Turn a selected row into the shape the API answers with."""
    return dict(zip(COLUMNS.replace(" ", "").split(","), row, strict=True))


# Renews or drops the lock only while it still names the run holding it.
_RENEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if ARGV[2] == '0' then return redis.call('DEL', KEYS[1]) end
return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
"""


def lock_key(project: str) -> str:
    """Return the key that says a run of this project is going."""
    return queue.key("index", "running", project)


def running_job(cursor: Cursor, project: str) -> dict[str, Any] | None:
    """Return the run still going for a project, if there is one.

    The lock in Valkey is what says so: a run renews it while it lives, so a
    dead process stops holding it within INDEX_LOCK_SECONDS.
    """
    held = queue.client().get(lock_key(project))
    if held is None or not held.isdigit():
        return None
    return job_row(cursor, int(held))


def renew_lock(project: str, job_id: int, seconds: int) -> bool:
    """Extend the lock of a run, or drop it when `seconds` is 0."""
    script = queue.client().register_script(_RENEW)
    return bool(script(keys=[lock_key(project)], args=[str(job_id), seconds]))


def job_row(cursor: Cursor, job_id: int) -> dict[str, Any] | None:
    """Return one run by id."""
    cursor.execute(f"SELECT {COLUMNS} FROM index_jobs WHERE id = %s;", (job_id,))
    row = cursor.fetchone()
    return row_view(row) if row else None


def recent_jobs(cursor: Cursor, project: str | None, limit: int) -> list[dict]:
    """Return the last runs, newest first, of one project or of all of them."""
    cursor.execute(
        f"SELECT {COLUMNS} FROM index_jobs "
        "WHERE (%s::text IS NULL OR project = %s) "
        "ORDER BY started_at DESC LIMIT %s;",
        (project, project, limit),
    )
    return [row_view(row) for row in cursor.fetchall()]


def open_job(
    cursor: Cursor, project: str, fresh: bool, project_type: str | None
) -> int:
    """Record a run about to start. Raises if one is already going."""
    cursor.execute(
        """
        INSERT INTO index_jobs (project, fresh, project_type)
        VALUES (%s, %s, %s)
        RETURNING id;
        """,
        (project, fresh, project_type),
    )
    return int(cursor.fetchone()[0])


def last_run(cursor: Cursor, project: str) -> datetime | None:
    """When a run for this project last started, whatever became of it.

    A failed run counts: the scheduler waits an interval after it rather than
    trying again on the next tick, so a project that cannot be indexed is not
    indexed once a minute forever.
    """
    cursor.execute(
        "SELECT MAX(started_at) FROM index_jobs WHERE project = %s;", (project,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def open_run(
    cursor: Cursor, project: str, project_type: str | None, fresh: bool
) -> dict[str, Any]:
    """Check that a project may start a run now, and record that it has.

    One implementation of "may this project be indexed", so a run the schedule
    started and a run the dashboard asked for cannot come to different
    conclusions. The thread is left to the caller: it outlives the transaction
    this is called in, and starting it before the row is committed would let a
    rollback leave a run nothing is tracking.
    """
    mount = project_mount(project)
    if not is_mounted(mount):
        raise RuntimeError(
            f"{project} is not mounted at {mount}; the override has to be "
            "rewritten, or the tree was recreated on the host, and this "
            "service has to be recreated before it can be read"
        )
    running = running_job(cursor, project)
    if running is not None:
        raise RuntimeError(f"job {running['id']} is already indexing this project")
    held = queue.client().set(lock_key(project), "", nx=True, ex=INDEX_LOCK_SECONDS)
    if not held:
        raise RuntimeError(f"a run of {project} is already starting")
    try:
        job_id = open_job(cursor, project, fresh, project_type)
    except Exception:
        queue.client().delete(lock_key(project))
        raise
    queue.client().set(lock_key(project), job_id, xx=True, keepttl=True)
    return job_row(cursor, job_id) or {"id": job_id}


def fail_orphaned(cursor: Cursor, before: datetime) -> list[tuple[int, str]]:
    """Close the runs an earlier process left behind, and name them.

    A run is a daemon thread of the API: a restart in the middle of one leaves
    the row `running` for ever and its lock alive until it expires. Indexing
    only ever happens in this process, so a row still running from before this
    process started belongs to a dead one, and so does its lock.
    """
    cursor.execute(
        """
        UPDATE index_jobs
           SET status = 'failed', finished_at = CURRENT_TIMESTAMP,
               error = 'worker-api restarted while this run was going'
         WHERE status = 'running' AND started_at < %s
        RETURNING id, project;
        """,
        (before,),
    )
    orphaned = [(int(row[0]), str(row[1])) for row in cursor.fetchall()]
    for job_id, project in orphaned:
        renew_lock(project, job_id, 0)
    return orphaned


def close_job(
    cursor: Cursor,
    job_id: int,
    counts: dict[str, int] | None,
    error: str | None,
) -> None:
    """Record how a run ended, whether it finished or threw."""
    values = [None if counts is None else counts.get(name) for name in COUNTS]
    cursor.execute(
        """
        UPDATE index_jobs
           SET status = %s, error = %s, finished_at = CURRENT_TIMESTAMP,
               files = %s, with_node = %s, entities = %s, edges = %s,
               pruned = %s, failures = %s, gaps = %s
         WHERE id = %s;
        """,
        ("failed" if error else "done", error, *values, job_id),
    )


def queue_embeddings(cursor: Cursor, project: str) -> None:
    """Enqueue what a finished run changed, when the project embeds itself.

    Here rather than in the loop so a file is queued the moment its hash
    moves: the loop's own sweep is coarse and exists for the other case, a
    project switched on long after it was last indexed.
    """
    settled = features.resolve(cursor, project, FEATURE_EMBEDDING)
    if not settled.enabled:
        return
    written = embedjobs.enqueue_project(
        cursor, project, EMBED_MODEL, settled.chunk_chars
    )
    if written:
        LOG.info("Queued %d file(s) of %s for embedding", written, project)


def run_in_background(
    job_id: int,
    project: str,
    root_path: str,
    project_type: str | None,
    fresh: bool,
) -> None:
    """Index a project on a thread of its own, and close the row after.

    A connection of its own, because the request that started this returned
    long ago and the pooled one went back with it.
    """

    def work() -> None:
        # Imported here rather than at module level: this pulls in the whole
        # tree-sitter stack, and the API must start whether or not a run is
        # ever asked for.
        from enggraph.indexer import scan_and_build_graph

        finished = threading.Event()
        threading.Thread(
            target=keep_lock,
            args=(project, job_id, finished),
            name=f"index-lock-{project}",
            daemon=True,
        ).start()
        counts: dict[str, int] | None = None
        error: str | None = None
        try:
            counts = scan_and_build_graph(project, root_path, project_type, fresh)
        except Exception as failure:  # noqa: BLE001 - recorded, not swallowed
            error = f"{type(failure).__name__}: {failure}"
            LOG.exception("Indexing %s failed", project)
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                close_job(cursor, job_id, counts, error)
            conn.commit()
            if error is None:
                # Its own transaction: a failure here used to roll back
                # close_job and leave the run `running` until a restart.
                try:
                    with conn.cursor() as cursor:
                        queue_embeddings(cursor, project)
                    conn.commit()
                except Exception:  # noqa: BLE001 - the loop's sweep retries it
                    conn.rollback()
                    LOG.exception("Could not queue %s for embedding", project)
        finally:
            conn.close()
            finished.set()
            try:
                renew_lock(project, job_id, 0)
            except valkey.ValkeyError:
                LOG.warning("Could not release the index lock of %s", project)

    threading.Thread(target=work, name=f"index-{project}", daemon=True).start()


def keep_lock(project: str, job_id: int, finished: threading.Event) -> None:
    """Renew the lock of a run until it finishes."""
    while not finished.wait(max(1, INDEX_LOCK_SECONDS // 3)):
        try:
            renew_lock(project, job_id, INDEX_LOCK_SECONDS)
        except valkey.ValkeyError:
            LOG.warning("Could not renew the index lock of %s", project)
