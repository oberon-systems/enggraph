"""The queue behind the embedding loop, kept in Valkey.

Which files are owed is still read from the graph: that part is a question
about Postgres and stays a SELECT. What is queued, leased, done or given up on
lives in Valkey under `eg:embed:<project>:*`, keyed by file path, so a file
that changes twice before it is embedded is one entry carrying the newer hash.

Every count the dashboard shows is a cardinality of these keys, so reading the
depth costs the same for ten files as for a hundred thousand.
"""

from __future__ import annotations

from typing import Any

from psycopg2.extensions import cursor as Cursor

from enggraph import queue
from enggraph.chunks import CHUNKER_REVISION
from enggraph.storage import SKIP_EMBED, clear_skip, mark_skip

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# pending ZSET path -> enqueue time, leased ZSET path -> lease deadline,
# hash/done/attempts/failed HASH path -> value. A done value is the hash, the
# version (model, window, chunker) and whether the file was empty: only an
# empty one, which leaves no chunk for the graph to see, is held back by it.
_ENQUEUE = """
local pending, leased, hashes, done, attempts, failed, version =
    KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5], KEYS[6], KEYS[7]
local now = tonumber(ARGV[1])
redis.call('SET', version, ARGV[2])
local written = 0
for i = 3, #ARGV, 2 do
    local path, digest = ARGV[i], ARGV[i + 1]
    local current = redis.call('HGET', hashes, path)
    local queued = redis.call('ZSCORE', pending, path)
        or redis.call('ZSCORE', leased, path)
    local settled = redis.call('HGET', done, path) == digest .. '|' .. ARGV[2] .. '|1'
    if not settled and not (queued and current == digest) then
        redis.call('HSET', hashes, path, digest)
        redis.call('ZADD', pending, 'NX', now + i / 1e6, path)
        redis.call('HDEL', done, path)
        redis.call('HDEL', attempts, path)
        redis.call('HDEL', failed, path)
        written = written + 1
    end
end
return written
"""

_CLAIM = """
local pending, leased, hashes, attempts = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local now, limit, lease = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
for _, path in ipairs(redis.call('ZRANGEBYSCORE', leased, '-inf', now)) do
    redis.call('ZREM', leased, path)
    redis.call('ZADD', pending, 'NX', now, path)
end
local taken = {}
for _, path in ipairs(redis.call('ZRANGE', pending, 0, limit - 1)) do
    redis.call('ZREM', pending, path)
    redis.call('ZADD', leased, now + lease, path)
    local tries = redis.call('HINCRBY', attempts, path, 1)
    local digest = redis.call('HGET', hashes, path) or ''
    table.insert(taken, {path, digest, tries})
end
return taken
"""

_FINISH = """
local leased, hashes, done, attempts, failed, version =
    KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5], KEYS[6]
local path, digest, empty = ARGV[1], ARGV[2], ARGV[3]
redis.call('ZREM', leased, path)
redis.call('HDEL', attempts, path)
redis.call('HDEL', failed, path)
if (redis.call('HGET', hashes, path) or digest) == digest then
    local stamp = digest .. '|' .. (redis.call('GET', version) or '') .. '|' .. empty
    redis.call('HSET', done, path, stamp)
end
return 1
"""

_RELEASE = """
local pending, leased, attempts = KEYS[1], KEYS[2], KEYS[3]
local path, now = ARGV[1], tonumber(ARGV[2])
if redis.call('ZREM', leased, path) == 1 then
    redis.call('ZADD', pending, 'NX', now, path)
    if tonumber(redis.call('HGET', attempts, path) or '0') > 0 then
        redis.call('HINCRBY', attempts, path, -1)
    end
end
return 1
"""

_FAIL = """
local pending, leased, attempts, failed = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local path, now, limit, error = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREM', leased, path)
if tonumber(redis.call('HGET', attempts, path) or '0') >= limit then
    redis.call('ZREM', pending, path)
    redis.call('HSET', failed, path, error)
    return 'failed'
end
redis.call('ZADD', pending, 'NX', now, path)
return 'pending'
"""


def keys(project: str) -> dict[str, str]:
    """Return the key of every structure of one project's queue."""
    return {
        name: queue.key("embed", project, name)
        for name in (
            "pending",
            "leased",
            "hash",
            "done",
            "attempts",
            "failed",
            "version",
        )
    }


def owed_files(
    cursor: Cursor, project: str, model: str, chunk_chars: int = 0
) -> list[tuple[str, str]]:
    """Return (path, hash) of every file whose vectors are not current.

    A file is current when every chunk of it was written from the hash the
    file has now, by the model in use, at the window in use. The list comes
    from the graph, because the graph is what coverage is measured against.
    """
    cursor.execute(
        """
        SELECT DISTINCT ON (n.file_path) n.file_path, COALESCE(h.hash, '')
          FROM graph_nodes AS n
          LEFT JOIN file_hashes AS h
            ON h.project = n.project AND h.file_path = n.file_path
         WHERE n.project = %s AND n.type = 'file' AND n.file_path IS NOT NULL
           AND (COALESCE((n.metadata ->> 'skip')::int, 0) & %s) = 0
           AND NOT EXISTS (
                 SELECT 1 FROM code_embeddings AS e
                  WHERE e.project = n.project AND e.node_id = n.id
                    AND e.kind = 'source'
                    AND e.content_hash = COALESCE(h.hash, '')
                    AND e.model = %s
                    AND (%s = 0 OR e.chunk_chars = %s)
                    AND e.chunker = %s
               )
         ORDER BY n.file_path;
        """,
        (project, SKIP_EMBED, model, chunk_chars, chunk_chars, CHUNKER_REVISION),
    )
    return [(str(path), str(digest)) for path, digest in cursor.fetchall()]


def enqueue_project(
    cursor: Cursor, project: str, model: str, chunk_chars: int = 0
) -> int:
    """Queue every file of a project whose vectors are not current.

    A file already queued with the same hash, or finished at that hash with
    nothing to embed, is left alone. Returns how many entries were written.
    """
    owed = owed_files(cursor, project, model, chunk_chars)
    if not owed:
        return 0
    names = keys(project)
    script = queue.client().register_script(_ENQUEUE)
    version = f"{model}:{chunk_chars}:{CHUNKER_REVISION}"
    written = 0
    for start in range(0, len(owed), 1000):
        flat = [value for pair in owed[start : start + 1000] for value in pair]
        written += int(
            script(
                keys=[
                    names["pending"],
                    names["leased"],
                    names["hash"],
                    names["done"],
                    names["attempts"],
                    names["failed"],
                    names["version"],
                ],
                args=[queue.now(), version, *flat],
            )
        )
    return written


def claim(projects: list[str], limit: int, lease_seconds: int) -> list[dict[str, Any]]:
    """Take up to `limit` files to embed from each project named.

    A lease that ran out is returned to the queue first, so a loop that died
    holding files releases them by the clock rather than by anyone noticing.
    """
    if not projects or limit <= 0:
        return []
    script = queue.client().register_script(_CLAIM)
    taken: list[dict[str, Any]] = []
    for project in projects:
        names = keys(project)
        rows = script(
            keys=[names["pending"], names["leased"], names["hash"], names["attempts"]],
            args=[queue.now(), limit, lease_seconds],
        )
        taken.extend(
            {
                "id": str(path),
                "project": project,
                "file_path": str(path),
                "content_hash": str(digest),
                "attempts": int(tries),
            }
            for path, digest, tries in rows
        )
    return taken


def finish(task: dict[str, Any], empty: bool = False) -> None:
    """Mark one file embedded at the hash it was claimed with."""
    names = keys(task["project"])
    queue.client().register_script(_FINISH)(
        keys=[
            names["leased"],
            names["hash"],
            names["done"],
            names["attempts"],
            names["failed"],
            names["version"],
        ],
        args=[task["file_path"], task["content_hash"], "1" if empty else "0"],
    )


def release(task: dict[str, Any]) -> None:
    """Hand one file back unattempted: no server answered, nobody's fault."""
    names = keys(task["project"])
    queue.client().register_script(_RELEASE)(
        keys=[names["pending"], names["leased"], names["attempts"]],
        args=[task["file_path"], queue.now()],
    )


def fail(cursor: Cursor, task: dict[str, Any], error: str, max_attempts: int) -> str:
    """Record what went wrong, and stop retrying a file that keeps doing it.

    Once the attempts are spent the file's embed skip bit is set in the graph,
    which keeps it out of every later sweep until someone retries the project.
    """
    names = keys(task["project"])
    state = str(
        queue.client().register_script(_FAIL)(
            keys=[
                names["pending"],
                names["leased"],
                names["attempts"],
                names["failed"],
            ],
            args=[task["file_path"], queue.now(), max_attempts, error[:1000]],
        )
    )
    if state == FAILED:
        mark_skip(cursor, task["project"], task["file_path"], SKIP_EMBED, error[:1000])
    return state


def drop_task(project: str, file_path: str) -> None:
    """Forget a file that is no longer in the graph."""
    names = keys(project)
    pipe = queue.client().pipeline()
    for name in ("pending", "leased"):
        pipe.zrem(names[name], file_path)
    for name in ("hash", "done", "attempts", "failed"):
        pipe.hdel(names[name], file_path)
    pipe.execute()


def retry_failed(cursor: Cursor, project: str | None = None) -> int:
    """Put the files that gave up back in the queue, attempts forgiven.

    The skip bit in the graph is cleared too, so the next sweep queues them.
    """
    projects = [project] if project else sorted(queue.projects_with_keys())
    forgiven = 0
    for name in projects:
        names = keys(name)
        paths = list(queue.client().hkeys(names["failed"]))
        if paths:
            pipe = queue.client().pipeline()
            pipe.hdel(names["failed"], *paths)
            pipe.hdel(names["attempts"], *paths)
            pipe.execute()
        forgiven += len(paths)
    return max(forgiven, clear_skip(cursor, project, SKIP_EMBED))


def done_paths(project: str) -> set[str]:
    """Return the files finished with nothing to embed."""
    marks = queue.client().hgetall(keys(project)["done"])
    return {path for path, stamp in marks.items() if stamp.endswith("|1")}


def queue_depth(project: str | None = None) -> dict[str, int]:
    """Count one project's files by state, or every project's.

    An expired lease counts as pending, which is what it is: it goes back the
    moment anyone claims.
    """
    projects = [project] if project else sorted(queue.projects_with_keys())
    depth = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0, "total": 0}
    moment = queue.now()
    pipe = queue.client().pipeline()
    for name in projects:
        names = keys(name)
        pipe.zcard(names["pending"])
        pipe.zcount(names["leased"], "-inf", moment)
        pipe.zcount(names["leased"], f"({moment}", "+inf")
        pipe.hlen(names["done"])
        pipe.hlen(names["failed"])
    counts = pipe.execute()
    for offset in range(0, len(counts), 5):
        pending, expired, running, done, failed = counts[offset : offset + 5]
        depth[PENDING] += int(pending) + int(expired)
        depth[RUNNING] += int(running)
        depth[DONE] += int(done)
        depth[FAILED] += int(failed)
    depth["total"] = sum(depth[state] for state in (PENDING, RUNNING, DONE, FAILED))
    return depth
