"""The queue behind the embedding loop, as SQL over a cursor.

Kept apart from `storage`, which is the indexer's half of the database, for
the reason `jobs` is: a task here describes a file already in the graph and
writes nothing an index run would recognise.

Keyed on the file rather than on a run. A file that changes twice before it is
embedded is one task carrying the newer hash, and a project switched on years
after it was indexed is enqueued by the same statement that enqueues a file
saved a second ago.
"""

from __future__ import annotations

from typing import Any

from psycopg2.extensions import cursor as Cursor

# What a task is doing. `running` is a claim held under a lease; a lease that
# has run out reads as pending again without anyone reclaiming it.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


def enqueue_project(
    cursor: Cursor, project: str, model: str, chunk_chars: int = 0
) -> int:
    """Enqueue every file of a project whose vectors are not current.

    A file is current when every chunk of it was written from the hash the
    file has now, by the model in use, at the window in use. Anything else -
    never embedded, edited since, embedded by another model, cut to another
    size - is work, and the statement does not care which of those it is.

    Enqueuing an already pending file moves its hash rather than adding a
    second row, so a file edited repeatedly costs one task.

    The list of files comes from the graph rather than from `file_hashes`,
    because the graph is what the coverage is measured against. Keyed on the
    hashes, a file the indexer recorded no hash for could never be queued and
    the percentage could never reach 100 - it would sit at whatever fraction
    happened to have one, which is exactly what it did.
    """
    cursor.execute(
        """
        INSERT INTO embed_tasks (project, file_path, content_hash, status)
        SELECT n.project, n.file_path, COALESCE(h.hash, ''), %s
          FROM graph_nodes AS n
          LEFT JOIN file_hashes AS h
            ON h.project = n.project AND h.file_path = n.file_path
         WHERE n.project = %s AND n.type = 'file' AND n.file_path IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1 FROM code_embeddings AS e
                  WHERE e.project = n.project AND e.node_id = n.id
                    AND e.content_hash = COALESCE(h.hash, '')
                    AND e.model = %s
                    AND (%s = 0 OR e.chunk_chars = %s)
               )
        ON CONFLICT (project, file_path) DO UPDATE
            SET content_hash = EXCLUDED.content_hash,
                status = %s,
                attempts = 0,
                lease_until = NULL,
                error = NULL,
                updated_at = CURRENT_TIMESTAMP
          WHERE embed_tasks.content_hash <> EXCLUDED.content_hash
        RETURNING id;
        """,
        (PENDING, project, model, chunk_chars, chunk_chars, PENDING),
    )
    return len(cursor.fetchall())


def claim(
    cursor: Cursor, projects: list[str], limit: int, lease_seconds: int
) -> list[dict[str, Any]]:
    """Take up to `limit` files to embed, for the projects named.

    Only the projects handed in: whether a feature is switched on is settled
    before the queue is read, so a project turned off keeps its tasks rather
    than having them drained by someone else's tick.

    SKIP LOCKED because this is meant to be safe to run in two processes, even
    though today it runs in one.
    """
    if not projects or limit <= 0:
        return []
    cursor.execute(
        """
        WITH taken AS (
            SELECT id FROM embed_tasks
             WHERE project = ANY(%s)
               AND (status = %s
                    OR (status = %s AND lease_until < CURRENT_TIMESTAMP))
             ORDER BY id
             LIMIT %s
             FOR UPDATE SKIP LOCKED
        )
        UPDATE embed_tasks AS t
           SET status = %s,
               attempts = t.attempts + 1,
               lease_until = CURRENT_TIMESTAMP + MAKE_INTERVAL(secs => %s),
               updated_at = CURRENT_TIMESTAMP
          FROM taken
         WHERE t.id = taken.id
        RETURNING t.id, t.project, t.file_path, t.content_hash, t.attempts;
        """,
        (projects, PENDING, RUNNING, limit, RUNNING, lease_seconds),
    )
    return [
        dict(
            zip(
                ("id", "project", "file_path", "content_hash", "attempts"),
                row,
                strict=False,
            )
        )
        for row in cursor.fetchall()
    ]


def finish(cursor: Cursor, task_id: int) -> None:
    """Mark one file embedded."""
    cursor.execute(
        """
        UPDATE embed_tasks
           SET status = %s, lease_until = NULL, error = NULL,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s;
        """,
        (DONE, task_id),
    )


def release(cursor: Cursor, task_id: int) -> None:
    """Hand one file back unattempted.

    For the case that is nobody's fault: no server answered. The attempt is
    given back too, so an afternoon with the model switched off does not
    exhaust the retries of every file in the queue.
    """
    cursor.execute(
        """
        UPDATE embed_tasks
           SET status = %s,
               attempts = GREATEST(0, attempts - 1),
               lease_until = NULL,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s;
        """,
        (PENDING, task_id),
    )


def fail(cursor: Cursor, task_id: int, error: str, max_attempts: int) -> None:
    """Record what went wrong, and stop retrying a file that keeps doing it."""
    cursor.execute(
        """
        UPDATE embed_tasks
           SET status = CASE WHEN attempts >= %s THEN %s ELSE %s END,
               lease_until = NULL,
               error = %s,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s;
        """,
        (max_attempts, FAILED, PENDING, error[:1000], task_id),
    )


def drop_task(cursor: Cursor, project: str, file_path: str) -> None:
    """Forget a file that is no longer in the graph."""
    cursor.execute(
        "DELETE FROM embed_tasks WHERE project = %s AND file_path = %s;",
        (project, file_path),
    )


def retry_failed(cursor: Cursor, project: str | None = None) -> int:
    """Put the files that gave up back in the queue, attempts forgiven.

    A task fails after its attempts run out, and nothing retries it: that is
    right when the file is the problem and wrong when the server was. Asked
    for by hand, because only a person knows which of the two it was.
    """
    cursor.execute(
        """
        UPDATE embed_tasks
           SET status = %s, attempts = 0, error = NULL, lease_until = NULL,
               updated_at = CURRENT_TIMESTAMP
         WHERE status = %s AND (%s::text IS NULL OR project = %s)
        RETURNING id;
        """,
        (PENDING, FAILED, project, project),
    )
    return len(cursor.fetchall())


def queue_depth(cursor: Cursor, project: str | None = None) -> dict[str, int]:
    """Count tasks by status, for one project or for all of them.

    An expired lease is counted as pending, which is what it is: the row says
    `running` only until someone reads it.
    """
    cursor.execute(
        """
        SELECT
            CASE
                WHEN status = %s AND lease_until < CURRENT_TIMESTAMP THEN %s
                ELSE status
            END AS state,
            COUNT(*)
          FROM embed_tasks
         WHERE %s::text IS NULL OR project = %s
         GROUP BY 1;
        """,
        (RUNNING, PENDING, project, project),
    )
    depth = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0, "total": 0}
    for status, count in cursor.fetchall():
        depth[status] = depth.get(status, 0) + int(count)
        depth["total"] += int(count)
    return depth
