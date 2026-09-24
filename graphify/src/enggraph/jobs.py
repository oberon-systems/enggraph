"""The work queue behind the summarizer, kept in Valkey.

A job is one project's worth of nodes to describe, a task is one node, and a
lease that expires without an answer is picked up by whoever asks next. What
is owed is read from the graph; the queue itself lives under `eg:sum:*`, and
every transition goes through one Lua script so the gauges a job carries move
with the task that moved them.

Any key here may be evicted. A lost job is opened again over what the graph
still owes, and a summary already written stays in Postgres either way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg2.extensions import cursor as Cursor

from enggraph import nodetext, queue
from enggraph.config import (
    LLM_INPUT_CHARS,
    SUMMARY_JOB_TTL_SECONDS,
    SUMMARY_JOB_WINDOW,
    WORKER_MAX_ATTEMPTS,
)
from enggraph.hierarchy import ROOT_ID, depth_of, parent_of
from enggraph.nodetext import DIRECTORY, FILE, Node
from enggraph.storage import SKIP_SUMMARIZE, clear_skip, mark_skip
from enggraph.summary_text import content_key

# A file the graph names but the mount does not hold: the graph is ahead of
# the tree, and re-indexing is what settles it.
NO_FILE = "not on the mount, re-index the project"
EMPTY_FILE = nodetext.EMPTY_FILE
SPENT = "attempts spent"
STATES = ("pending", "leased", "done", "failed", "skipped")
# One task is one string of these fields joined by \x1f, in this order.
TASK_CODE = ("s", "a", "r", "h", "t", "w", "o", "x", "n", "k", "f", "u")
SEPARATOR = "\x1f"

_LIB = """
local unpack = unpack or table.unpack
local F = {'s','a','r','h','t','w','o','x','n','k','f','u'}
local function jkey(job, part) return 'eg:sum:job:' .. job .. ':' .. part end
local function decode(raw)
    local t, i = {}, 1
    for part in (raw .. '\\31'):gmatch('(.-)\\31') do t[F[i]] = part; i = i + 1 end
    t.a = tonumber(t.a); t.r = tonumber(t.r)
    return t
end
local function encode(t)
    local out = {}
    for i, name in ipairs(F) do out[i] = tostring(t[name] or '') end
    return table.concat(out, '\\31')
end
local function load(job, tid)
    local raw = redis.call('HGET', jkey(job, 'task'), tid)
    if not raw then return nil end
    return decode(raw)
end
local function count(job, state, by)
    redis.call('HINCRBY', jkey(job, 'count'), state, by)
end
-- Moves one task, and fails it instead of queueing it once its attempts are spent.
local function move(job, tid, t, to, now, limit, expiry)
    if to == 'pending' and t.a >= limit then
        to = 'failed'
        if t.x == '' then t.x = 'attempts spent' end
        redis.call('SADD', jkey(job, 'spent'), tid)
    end
    local from = t.s
    if from == 'pending' then
        redis.call('ZREM', jkey(job, 'pending'), tid)
        redis.call('SREM', jkey(job, 'hashed'), tid)
    elseif from == 'leased' then
        redis.call('ZREM', jkey(job, 'leased'), tid)
    end
    if to == 'pending' then
        redis.call('ZADD', jkey(job, 'pending'), t.r, tid)
        if t.h ~= '' then redis.call('SADD', jkey(job, 'hashed'), tid) end
    elseif to == 'leased' then
        redis.call('ZADD', jkey(job, 'leased'), expiry, tid)
    end
    if to ~= 'leased' then t.t = ''; t.w = '' end
    if from ~= to then count(job, from, -1); count(job, to, 1) end
    t.s = to; t.u = now
    -- A finished task lives on in the counts only; the job's memory is its window.
    if to == 'done' then
        redis.call('HDEL', jkey(job, 'task'), tid)
        redis.call('HDEL', 'eg:sum:taskjob', tid)
        return to
    end
    redis.call('HSET', jkey(job, 'task'), tid, encode(t))
    return to
end
local function reclaim(job, now, limit)
    local moved = 0
    local expired = redis.call('ZRANGEBYSCORE', jkey(job, 'leased'), '-inf', now)
    for _, tid in ipairs(expired) do
        local t = load(job, tid)
        if t then move(job, tid, t, 'pending', now, limit); moved = moved + 1
        else redis.call('ZREM', jkey(job, 'leased'), tid) end
    end
    return moved
end
local function close(job, status, now, ttl)
    local jobkey = 'eg:sum:job:' .. job
    if redis.call('HGET', jobkey, 'status') ~= 'running' then return 0 end
    redis.call('HSET', jobkey, 'status', status, 'finished_at', now, 'updated_at', now)
    local project = redis.call('HGET', jobkey, 'project')
    if redis.call('HGET', 'eg:sum:running', project) == job then
        redis.call('HDEL', 'eg:sum:running', project)
    end
    local tids = redis.call('HKEYS', jkey(job, 'task'))
    for i = 1, #tids, 5000 do
        redis.call('HDEL', 'eg:sum:taskjob', unpack(tids, i, math.min(i + 4999, #tids)))
    end
    redis.call('HSET', jkey(job, 'count'), 'pending', 0, 'leased', 0)
    redis.call('DEL', jkey(job, 'task'), jkey(job, 'pending'), jkey(job, 'leased'),
               jkey(job, 'hashed'), jkey(job, 'spent'), jkey(job, 'seen'))
    redis.call('EXPIRE', jobkey, ttl)
    redis.call('EXPIRE', jkey(job, 'count'), ttl)
    return 1
end
"""

_CREATE = """
local project, now_ms, stamp = ARGV[1], tonumber(ARGV[2]), ARGV[3]
local open = redis.call('HGET', 'eg:sum:running', project)
if open and redis.call('HGET', 'eg:sum:job:' .. open, 'status') == 'running' then
    return -tonumber(open)
end
local id = redis.call('INCR', 'eg:sum:seq')
if id < now_ms then id = now_ms; redis.call('SET', 'eg:sum:seq', id) end
redis.call('HSET', 'eg:sum:job:' .. id, 'id', id, 'project', project,
           'status', 'running', 'input_chars', ARGV[4], 'refresh', ARGV[5],
           'lease_seconds', ARGV[6], 'model', ARGV[7],
           'created_at', stamp, 'updated_at', stamp, 'finished_at', '')
redis.call('HSET', 'eg:sum:job:' .. id .. ':count', 'pending', 0, 'leased', 0,
           'done', 0, 'failed', 0, 'skipped', 0, 'total', 0,
           'from_cache', 0, 'from_model', 0)
redis.call('HSET', 'eg:sum:running', project, id)
redis.call('ZADD', 'eg:sum:jobs', id, id)
return id
"""

_NEXT_IDS = """
local first = redis.call('INCRBY', 'eg:sum:seq', tonumber(ARGV[1]))
local floor = tonumber(ARGV[2])
if first - tonumber(ARGV[1]) < floor then
    first = floor + tonumber(ARGV[1]); redis.call('SET', 'eg:sum:seq', first)
end
return first - tonumber(ARGV[1]) + 1
"""

_CLAIM = (
    _LIB
    + """
local job, batch, token, worker = ARGV[1], tonumber(ARGV[2]), ARGV[3], ARGV[4]
local lease, limit, now = tonumber(ARGV[5]), tonumber(ARGV[6]), tonumber(ARGV[7])
if redis.call('HGET', 'eg:sum:job:' .. job, 'status') ~= 'running' then return {} end
reclaim(job, now, limit)
local taken = {}
for _, tid in ipairs(redis.call('ZRANGE', jkey(job, 'pending'), 0, batch - 1)) do
    local t = load(job, tid)
    if t then
        t.a = t.a + 1; t.t = token; t.w = worker
        move(job, tid, t, 'leased', now, limit, now + lease)
        redis.call('HSET', 'eg:sum:lease:' .. token, 'job', job, tid, 1)
        table.insert(taken, tid .. '\\31' .. encode(t))
    else
        redis.call('ZREM', jkey(job, 'pending'), tid)
    end
end
if #taken > 0 then redis.call('EXPIRE', 'eg:sum:lease:' .. token, lease * 2 + 60) end
return taken
"""
)

_RECLAIM = (
    _LIB
    + """
return reclaim(ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]))
"""
)

_SPENT = (
    _LIB
    + """
local job = ARGV[1]
reclaim(job, tonumber(ARGV[2]), tonumber(ARGV[3]))
local out = {}
for _, tid in ipairs(redis.call('SMEMBERS', jkey(job, 'spent'))) do
    local t = load(job, tid)
    if t then table.insert(out, tid .. '\\31' .. encode(t)) end
end
redis.call('DEL', jkey(job, 'spent'))
return out
"""
)

# ARGV: tid, target, origin, note, now, limit, token ('' = any), mode, keep note.
# mode: '' plain, 'fail' by attempts, 'back' returns the attempt, 'retry' zeroes it.
_MOVE = (
    _LIB
    + """
local tid, target, origin, note = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local now, limit, token, mode = ARGV[5], tonumber(ARGV[6]), ARGV[7], ARGV[8]
local keep = ARGV[9] == '1'
local job = redis.call('HGET', 'eg:sum:taskjob', tid)
if not job then return '' end
local t = load(job, tid)
if not t then return '' end
if token ~= '' and (t.s ~= 'leased' or t.t ~= token) then return '' end
if mode == 'fail' then
    if t.a >= limit then target = 'failed' else target = 'pending' end
elseif mode == 'back' and t.a > 0 then
    t.a = t.a - 1
elseif mode == 'retry' then
    t.a = 0
end
if origin ~= '' then
    t.o = origin
    redis.call('HINCRBY', jkey(job, 'count'), 'from_' .. origin, 1)
end
if not keep then t.x = note end
return move(job, tid, t, target, now, limit)
"""
)

_LEASE = (
    _LIB
    + """
local token, op, now = ARGV[1], ARGV[2], tonumber(ARGV[3])
local seconds, limit = tonumber(ARGV[4]), tonumber(ARGV[5])
local lease = 'eg:sum:lease:' .. token
local job = redis.call('HGET', lease, 'job')
if not job or job ~= ARGV[6] then return 0 end
local touched = 0
for _, tid in ipairs(redis.call('HKEYS', lease)) do
    if tid ~= 'job' then
        local t = load(job, tid)
        if t and t.s == 'leased' and t.t == token then
            if op == 'extend' then
                redis.call('ZADD', jkey(job, 'leased'), 'XX', now + seconds, tid)
            else
                if op == 'back' and t.a > 0 then t.a = t.a - 1 end
                move(job, tid, t, 'pending', now, limit)
            end
            touched = touched + 1
        end
    end
end
if op == 'extend' then redis.call('EXPIRE', lease, seconds * 2 + 60)
else redis.call('DEL', lease) end
return touched
"""
)

_CLOSE = (
    _LIB
    + """
local job, status, now = ARGV[1], ARGV[2], ARGV[3]
local ttl, force = tonumber(ARGV[4]), ARGV[5]
if force ~= '1' then
    if redis.call('ZCARD', jkey(job, 'pending')) > 0 then return 0 end
    if redis.call('ZCARD', jkey(job, 'leased')) > 0 then return 0 end
end
return close(job, status, now, ttl)
"""
)


def stamp() -> str:
    """Return now as the ISO string every timestamp here is kept as."""
    return datetime.now(UTC).isoformat()


def _run(script: str, *args: object) -> object:
    """Run one of the scripts above with the given arguments."""
    return queue.client().register_script(script)(keys=[], args=list(args))


def _number(script: str, *args: object) -> int:
    """Run a script that answers a number."""
    value = _run(script, *args)
    return int(value) if isinstance(value, int | str | bytes) else 0


def _text(script: str, *args: object) -> str:
    """Run a script that answers a string."""
    value = _run(script, *args)
    return value if isinstance(value, str) else ""


def _rows(script: str, *args: object) -> list[str]:
    """Run a script that answers a list of encoded tasks."""
    value = _run(script, *args)
    return [str(row) for row in value] if isinstance(value, list) else []


def _decode(raw: str) -> dict[str, Any]:
    """Turn a task id and its separated fields into the shape callers read."""
    tid, *values = raw.split(SEPARATOR)
    fields = dict(zip(TASK_CODE, values, strict=False))
    return {
        "task_id": int(tid),
        "state": fields.get("s", ""),
        "attempts": int(fields.get("a") or 0),
        "content_hash": fields.get("h", ""),
        "lease_token": fields.get("t") or None,
        "worker_id": fields.get("w") or None,
        "origin": fields.get("o") or None,
        "note": fields.get("x") or None,
        "node_id": fields.get("n", ""),
        "kind": fields.get("k") or FILE,
        "file_path": fields.get("f", ""),
        "updated_at": _iso(fields.get("u")),
    }


def _iso(value: str | None) -> str | None:
    """Turn epoch seconds as stored by a script into an ISO string."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    except ValueError:
        return value


def task_node(task: dict[str, Any]) -> Node:
    """Return the node a task is about."""
    return Node(
        str(task.get("node_id") or task["file_path"]),
        str(task.get("kind") or FILE),
        str(task["file_path"]),
    )


def mark(cursor: Cursor, project: str, node: Node, reason: str) -> None:
    """Set the summarize skip bit on the node a task gave up on."""
    mark_skip(
        cursor,
        project,
        node.file_path,
        SKIP_SUMMARIZE,
        reason,
        None if node.kind == FILE else node.node_id,
    )


def job_row(job_id: int) -> dict[str, Any] | None:
    """Return one job, without its progress."""
    raw = queue.client().hgetall(queue.key("sum", "job", job_id))
    if not raw:
        return None
    return {
        "id": int(raw.get("id") or job_id),
        "project": raw.get("project", ""),
        "status": raw.get("status", ""),
        "input_chars": int(raw.get("input_chars") or LLM_INPUT_CHARS),
        "refresh": raw.get("refresh") == "1",
        "lease_seconds": int(raw.get("lease_seconds") or 0),
        "model": raw.get("model") or None,
        "created_at": raw.get("created_at") or None,
        "updated_at": raw.get("updated_at") or None,
        "finished_at": raw.get("finished_at") or None,
        "limit": int(raw.get("limit") or 0),
        "exhausted": raw.get("exhausted") == "1",
    }


def running_job(project: str) -> dict[str, Any] | None:
    """Return the job still handing out work for a project, if there is one."""
    open_id = queue.client().hget(queue.key("sum", "running"), project)
    if open_id is None:
        return None
    row = job_row(int(open_id))
    if row is None or row["status"] != "running":
        queue.client().hdel(queue.key("sum", "running"), project)
        return None
    return row


def create_job(
    project: str,
    input_chars: int,
    refresh: bool,
    lease_seconds: int,
    model: str | None,
) -> int:
    """Open a job. Raises RuntimeError when one is already open."""
    now = datetime.now(UTC)
    job_id = _number(
        _CREATE,
        project,
        int(now.timestamp() * 1000),
        now.isoformat(),
        input_chars,
        "1" if refresh else "0",
        lease_seconds,
        model or "",
    )
    if job_id < 0:
        raise RuntimeError(f"job {-job_id} is still running for {project}")
    return job_id


def owed_directories(
    cursor: Cursor,
    project: str,
    refresh: bool,
    changing: list[str],
    input_chars: int,
) -> list[str]:
    """Return the directories whose model summary is missing or out of date."""
    cursor.execute(
        """
        SELECT id,
               COALESCE(metadata ->> 'summary_source', 'auto'),
               COALESCE(metadata ->> 'summary_input', ''),
               (COALESCE((metadata ->> 'skip')::int, 0) & %s) <> 0
          FROM graph_nodes
         WHERE project = %s AND type = %s;
        """,
        (SKIP_SUMMARIZE, project, DIRECTORY),
    )
    rows = {
        str(row[0]): (str(row[1]), str(row[2]), bool(row[3]))
        for row in cursor.fetchall()
    }
    texts = nodetext.directory_texts(cursor, project, input_chars)
    owed: set[str] = set()
    for directory, (source, described, skipped) in rows.items():
        if source == "manual" or skipped:
            continue
        stale = described != content_key(texts.get(directory, ""))
        if source == "auto" or refresh or stale:
            owed.add(directory)
    for start in [*changing, *owed]:
        node = start
        while node != ROOT_ID:
            node = parent_of(node)
            source, _, skipped = rows.get(node, ("manual", "", True))
            if source != "manual" and not skipped:
                owed.add(node)
    return sorted(owed)


def owed_nodes(
    cursor: Cursor,
    project: str,
    refresh: bool,
    limit: int = 0,
    input_chars: int = LLM_INPUT_CHARS,
) -> list[tuple[str, str, str, int]]:
    """Return (node_id, kind, file_path, rank) of every node owed a summary.

    Files first, then directories deepest first, then entities: a directory is
    described from its children, and an entity reads better once its file has.
    """
    wanted = ["auto", "llm"] if refresh else ["auto"]
    cursor.execute(
        """
        SELECT n.id, n.file_path
          FROM graph_nodes AS n
         WHERE n.project = %s AND n.type = 'file' AND n.file_path IS NOT NULL
           AND COALESCE(n.metadata ->> 'summary_source', 'auto') = ANY(%s)
           AND (COALESCE((n.metadata ->> 'skip')::int, 0) & %s) = 0
         ORDER BY n.file_path
         LIMIT %s;
        """,
        (project, wanted, SKIP_SUMMARIZE, limit or None),
    )
    files = [(str(node_id), str(path)) for node_id, path in cursor.fetchall()]
    owed = [(node_id, FILE, path, nodetext.FILE_RANK) for node_id, path in files]

    directories = owed_directories(
        cursor, project, refresh, [node_id for node_id, _ in files], input_chars
    )
    owed.extend(
        (node_id, DIRECTORY, node_id, nodetext.DIRECTORY_RANK - depth_of(node_id))
        for node_id in directories
    )

    cursor.execute(
        """
        SELECT n.id, n.file_path
          FROM graph_nodes AS n
         WHERE n.project = %s AND n.file_path IS NOT NULL
           AND n.type NOT IN ('file', 'directory')
           AND n.id ~ '@L[0-9]+$'
           AND COALESCE(n.metadata ->> 'summary_source', 'auto') = ANY(%s)
           AND (COALESCE((n.metadata ->> 'skip')::int, 0) & %s) = 0
         ORDER BY n.file_path, n.id
         LIMIT %s;
        """,
        (project, wanted, SKIP_SUMMARIZE, limit or None),
    )
    owed.extend(
        (str(node_id), nodetext.ENTITY, str(path), nodetext.ENTITY_RANK)
        for node_id, path in cursor.fetchall()
    )
    return owed


def populate_job(
    cursor: Cursor,
    job_id: int,
    project: str,
    refresh: bool,
    limit: int = 0,
    input_chars: int = LLM_INPUT_CHARS,
) -> int:
    """Enqueue the first window of what a project owes. Returns the count.

    A job with a `limit` is that many nodes and no more; without one it holds
    SUMMARY_JOB_WINDOW at a time and `top_up` refills it as it drains.
    """
    queue.client().hset(queue.key("sum", "job", job_id), "limit", limit)
    owed = owed_nodes(cursor, project, refresh, limit, input_chars)
    return _fill(job_id, owed, limit or SUMMARY_JOB_WINDOW)


def top_up(cursor: Cursor, job_id: int) -> int:
    """Refill a running job from the graph once half its window is spent.

    A node the job has already held is never queued again by it, so a node
    whose summary could not be written does not come round for ever. Once a
    fill comes back short the graph has nothing more for this job, and it is
    not scanned again: that scan reads every directory of the project.
    """
    job = job_row(job_id)
    if job is None or job["status"] != "running" or job.get("limit"):
        return 0
    if job.get("exhausted"):
        return 0
    client = queue.client()
    held = int(client.zcard(queue.key("sum", "job", job_id, "pending")))
    if held >= SUMMARY_JOB_WINDOW // 2:
        return 0
    seen = client.smembers(queue.key("sum", "job", job_id, "seen"))
    owed = owed_nodes(cursor, job["project"], job["refresh"], 0, job["input_chars"])
    fresh = [node for node in owed if node[0] not in seen]
    return _fill(job_id, fresh, SUMMARY_JOB_WINDOW - held)


def _fill(job_id: int, owed: list[tuple[str, str, str, int]], room: int) -> int:
    """Write up to `room` owed nodes into a job as pending tasks. Returns the count.

    Every task starts with an empty digest: its text lives on the tree or in
    the graph, so what it hashes to is only known when it is claimed.
    """
    if len(owed) <= room:
        queue.client().hset(queue.key("sum", "job", job_id), "exhausted", 1)
    owed = owed[:room]
    if not owed:
        return 0
    floor = int(datetime.now(UTC).timestamp() * 1000)
    first = _number(_NEXT_IDS, len(owed), floor)
    moment = str(queue.now())
    tasks: dict[str, str] = {}
    pending: dict[str, float] = {}
    for ordinal, (node_id, kind, path, rank) in enumerate(owed):
        tid = str(first + ordinal)
        score = rank * 10_000_000 + ordinal
        fields = ("pending", "0", str(score), "", "", "", "", "", node_id, kind, path)
        tasks[tid] = SEPARATOR.join((*fields, moment))
        pending[tid] = score
    ids = list(tasks)
    pipe = queue.client().pipeline(transaction=True)
    for start in range(0, len(ids), 5000):
        chunk = ids[start : start + 5000]
        pipe.hset(
            queue.key("sum", "job", job_id, "task"),
            mapping={tid: tasks[tid] for tid in chunk},
        )
        pipe.zadd(
            queue.key("sum", "job", job_id, "pending"),
            {tid: pending[tid] for tid in chunk},
        )
        pipe.hset(queue.key("sum", "taskjob"), mapping=dict.fromkeys(chunk, job_id))
    for start in range(0, len(owed), 5000):
        pipe.sadd(
            queue.key("sum", "job", job_id, "seen"),
            *(node_id for node_id, *_ in owed[start : start + 5000]),
        )
    pipe.hincrby(queue.key("sum", "job", job_id, "count"), "pending", len(owed))
    pipe.hincrby(queue.key("sum", "job", job_id, "count"), "total", len(owed))
    pipe.execute()
    return len(owed)


def settle_cached(
    cursor: Cursor, job_id: int, project: str
) -> list[tuple[int, Node, str, str]]:
    """Close every pending task the cache can already answer.

    Only a task that was handed out once carries a digest, so only those are
    looked up. Returns (task id, node, summary, digest) for the caller to apply.
    """
    tids = list(queue.client().smembers(queue.key("sum", "job", job_id, "hashed")))
    if not tids:
        return []
    raws = queue.client().hmget(queue.key("sum", "job", job_id, "task"), tids)
    tasks = [
        _decode(f"{tid}{SEPARATOR}{raw}")
        for tid, raw in zip(tids, raws, strict=True)
        if raw
    ]
    digests = {task["content_hash"] for task in tasks if task["content_hash"]}
    if not digests:
        return []
    cursor.execute(
        "SELECT content_hash, summary FROM summary_cache "
        "WHERE project = %s AND content_hash = ANY(%s);",
        (project, list(digests)),
    )
    cached = {str(digest): str(summary) for digest, summary in cursor.fetchall()}
    settled: list[tuple[int, Node, str, str]] = []
    for task in tasks:
        summary = cached.get(task["content_hash"])
        if summary is None:
            continue
        if _move(task["task_id"], "done", origin="cache"):
            settled.append(
                (task["task_id"], task_node(task), summary, task["content_hash"])
            )
    return settled


def job_progress(job_id: int) -> dict[str, int]:
    """Count the tasks of a job by state.

    A lease that has run out reads as pending here, which is what makes a
    lazily reclaimed queue honest between one claim and the next.
    """
    client = queue.client()
    raw = client.hgetall(queue.key("sum", "job", job_id, "count"))
    progress = {
        name: max(0, int(raw.get(name) or 0))
        for name in (*STATES, "total", "from_cache", "from_model")
    }
    expired = int(
        client.zcount(queue.key("sum", "job", job_id, "leased"), "-inf", queue.now())
    )
    progress["leased"] = max(0, progress["leased"] - expired)
    progress["pending"] += expired
    return progress


def retry_failed(cursor: Cursor, project: str | None = None) -> int:
    """Put the files that gave up back in the queue, attempts forgiven.

    Only open jobs hold tasks; the skip bit is cleared as well, which is what
    brings a node given up on by a closed job into the next one.
    """
    client = queue.client()
    running = client.hgetall(queue.key("sum", "running"))
    forgiven = 0
    for name, job_id in running.items():
        if project is not None and name != project:
            continue
        for tid, raw in client.hscan_iter(queue.key("sum", "job", job_id, "task")):
            if raw.startswith("failed" + SEPARATOR):
                if _move(int(tid), "pending", note="", mode="retry"):
                    forgiven += 1
    return max(forgiven, clear_skip(cursor, project, SKIP_SUMMARIZE))


def queue_depth(project: str | None = None) -> dict[str, int]:
    """Count the tasks still owed across every open job, by state."""
    running = queue.client().hgetall(queue.key("sum", "running"))
    depth = {state: 0 for state in STATES} | {"total": 0}
    for name, job_id in running.items():
        if project is not None and name != project:
            continue
        progress = job_progress(int(job_id))
        for state in (*STATES, "total"):
            depth[state] += progress[state]
    return depth


def list_jobs(
    project: str | None, status: str | None, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    """List jobs newest first, with the total the page was taken from.

    A finished job is kept for SUMMARY_JOB_TTL_SECONDS; one whose key is gone
    is dropped from the index as it is met.
    """
    client = queue.client()
    ids = [int(job_id) for job_id in client.zrevrange(queue.key("sum", "jobs"), 0, -1)]
    rows: list[dict[str, Any]] = []
    gone: list[int] = []
    for job_id in ids:
        row = job_row(job_id)
        if row is None:
            gone.append(job_id)
            continue
        if project is not None and row["project"] != project:
            continue
        if status is not None and row["status"] != status:
            continue
        rows.append(row)
    if gone:
        client.zrem(queue.key("sum", "jobs"), *gone)
    return rows[offset : offset + limit], len(rows)


def list_job_files(
    job_id: int, state: str | None, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    """Page through the unfinished tasks of an open job. Never returns their text.

    A finished task is dropped from the job's window and survives only in its
    counts, so `done` pages are empty by design.
    """
    client = queue.client()
    raw = client.hgetall(queue.key("sum", "job", job_id, "task"))
    tasks = [_decode(f"{tid}{SEPARATOR}{value}") for tid, value in raw.items()]
    if state is not None:
        tasks = [task for task in tasks if task["state"] == state]
    scores = {tid: float(value.split(SEPARATOR)[2] or 0) for tid, value in raw.items()}
    tasks.sort(key=lambda task: (scores[str(task["task_id"])], task["task_id"]))
    page = tasks[offset : offset + limit]
    leased = queue.key("sum", "job", job_id, "leased")
    for task in page:
        expiry = client.zscore(leased, task["task_id"])
        task["lease_expires_at"] = _iso(str(expiry)) if expiry else None
        task.pop("lease_token", None)
        task.pop("content_hash", None)
    return page, len(tasks)


def cancel_job(job_id: int) -> bool:
    """Stop handing out work. A lease still held is refused when it answers."""
    closed = _number(_CLOSE, job_id, "cancelled", stamp(), SUMMARY_JOB_TTL_SECONDS, "1")
    return bool(closed)


def reclaim_expired(job_id: int, max_attempts: int = WORKER_MAX_ATTEMPTS) -> int:
    """Return leases that ran out to the queue.

    Run at the head of a claim rather than on a timer: the only moment a
    stale lease matters is when someone is asking for work.
    """
    return _number(_RECLAIM, job_id, queue.now(), max_attempts)


def fail_spent(cursor: Cursor, job_id: int, project: str, max_attempts: int) -> int:
    """Mark the nodes whose tasks ran out of attempts since the last claim."""
    spent = [_decode(raw) for raw in _rows(_SPENT, job_id, queue.now(), max_attempts)]
    for task in spent:
        mark(cursor, project, task_node(task), str(task["note"] or SPENT))
    return len(spent)


def claim_batch(
    job_id: int,
    batch: int,
    token: str,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
) -> list[dict[str, Any]]:
    """Take up to `batch` pending tasks under one lease token."""
    rows = _rows(
        _CLAIM,
        job_id,
        batch,
        token,
        worker_id,
        lease_seconds,
        max_attempts,
        queue.now(),
    )
    return [_decode(raw) for raw in rows]


def read_task_content(
    cursor: Cursor, project: str, tasks: list[dict[str, Any]], input_chars: int
) -> dict[int, tuple[str, str]]:
    """Read the text of the claimed tasks: (text, why there is none)."""
    return {
        int(task["task_id"]): nodetext.read(
            cursor, project, task_node(task), input_chars
        )
        for task in tasks
    }


def _move(
    task_id: int,
    target: str,
    origin: str = "",
    note: str | None = None,
    token: str = "",
    mode: str = "",
    max_attempts: int = WORKER_MAX_ATTEMPTS,
) -> str:
    """Move one task, returning its new state or '' when it is not there."""
    return _text(
        _MOVE,
        task_id,
        target,
        origin,
        note or "",
        queue.now(),
        max_attempts,
        token,
        mode,
        "1" if note is None else "0",
    )


def fail_task(task_id: int, error: str, max_attempts: int) -> str:
    """Send a task back to the queue, or give up on it. Returns the state."""
    return _move(task_id, "", note=error, mode="fail", max_attempts=max_attempts) or (
        "unknown"
    )


def fail_and_mark(
    cursor: Cursor,
    task_id: int,
    project: str,
    node: Node,
    error: str,
    max_attempts: int,
) -> str:
    """Fail one task, and mark its node once the attempts are spent."""
    state = fail_task(task_id, error, max_attempts)
    if state == "failed":
        mark(cursor, project, node, error)
    return state


def set_task_hash(task_id: int, content_hash: str) -> None:
    """Record the digest of the text actually handed out."""
    client = queue.client()
    job_id = client.hget(queue.key("sum", "taskjob"), task_id)
    if job_id is None:
        return
    name = queue.key("sum", "job", job_id, "task")
    raw = client.hget(name, task_id)
    if raw is None:
        return
    fields = raw.split(SEPARATOR)
    fields[TASK_CODE.index("h")] = content_hash
    client.hset(name, task_id, SEPARATOR.join(fields))


def settle_task(task_id: int) -> None:
    """Close one task the cache could already answer."""
    _move(task_id, "done", origin="cache")


def finish_task(task_id: int, note: str | None) -> None:
    """Mark a task answered by the model."""
    _move(task_id, "done", origin="model", note=note or "")


def lock_leased_task(task_id: int, token: str) -> dict[str, Any] | None:
    """Return the task a result is about, but only if the lease still holds.

    A worker that comes back after its lease expired finds nothing here, which
    is what stops it overwriting the answer of whoever took the task next.
    """
    client = queue.client()
    job_id = client.hget(queue.key("sum", "taskjob"), task_id)
    if job_id is None:
        return None
    raw = client.hget(queue.key("sum", "job", job_id, "task"), task_id)
    if raw is None:
        return None
    task = _decode(f"{task_id}{SEPARATOR}{raw}")
    if task["state"] != "leased" or task["lease_token"] != token:
        return None
    job = job_row(int(job_id)) or {}
    return {
        **task,
        "job_id": int(job_id),
        "project": job.get("project", ""),
        "job_status": job.get("status", ""),
    }


def extend_lease(job_id: int, token: str, lease_seconds: int) -> int:
    """Push back the deadline of a whole batch."""
    return _number(
        _LEASE, token, "extend", queue.now(), lease_seconds, WORKER_MAX_ATTEMPTS, job_id
    )


def release_lease(job_id: int, token: str) -> int:
    """Hand an unfinished batch straight back, without waiting it out."""
    return _number(
        _LEASE, token, "release", queue.now(), 0, WORKER_MAX_ATTEMPTS, job_id
    )


def hand_back(job_id: int, token: str) -> int:
    """Release a batch and return the attempt it spent.

    For the case that is nobody's fault: the server the loop pushes at did not
    answer, and an afternoon with the model switched off must not exhaust the
    retries of every file in the queue.
    """
    return _number(_LEASE, token, "back", queue.now(), 0, WORKER_MAX_ATTEMPTS, job_id)


def finish_job_if_drained(cursor: Cursor, job_id: int) -> bool:
    """Close a job once nothing is left to hand out, and let it expire.

    The window is topped up first, so a job closes when the graph owes nothing
    more rather than when its first window runs out.
    """
    top_up(cursor, job_id)
    closed = _number(_CLOSE, job_id, "done", stamp(), SUMMARY_JOB_TTL_SECONDS, "0")
    return bool(closed)
