"""The summary queue in Valkey: jobs, leases, attempts and the gauges they move.

What a job is filled with is a statement over the graph, answered here by a
cursor that hands back set rows. Everything after that runs against the
in-process Valkey from conftest.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from psycopg2.extensions import cursor as Cursor

from enggraph import jobs, nodetext, queue
from enggraph.storage import SKIP_SUMMARIZE


class Graph:
    """A cursor answering the owed-node statements of populate_job in order."""

    def __init__(
        self,
        files: list[str],
        entities: list[tuple[str, str]] | None = None,
        cached: list[tuple[str, str]] | None = None,
    ) -> None:
        """Take the file paths, the (entity id, path) pairs and cache rows."""
        self.files = files
        self.entities = entities or []
        self.cached = cached or []
        self.sent: list[str] = []
        self.params: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Keep what would have been sent."""
        self.sent.append(" ".join(sql.split()))
        self.params.append(params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Answer by what the last statement asked for."""
        sql = self.sent[-1]
        if "FROM summary_cache" in sql:
            return self.cached
        if "n.type = 'file'" in sql:
            return [(path, path) for path in self.files]
        if "n.type NOT IN ('file', 'directory')" in sql:
            return self.entities
        return []


def graph(*files: str, **kwargs: list[tuple[str, str]]) -> Cursor:
    """Return a Graph standing in for a psycopg2 cursor."""
    return cast(Cursor, Graph(list(files), **kwargs))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pin the queue's clock to a value the test moves by hand."""
    moment = [1000.0]
    monkeypatch.setattr(queue, "now", lambda: moment[0])
    return moment


@pytest.fixture
def marks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Record the skip marks written, instead of writing them."""
    marked: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        jobs,
        "mark_skip",
        lambda cursor, project, path, bit, reason, node_id: marked.append(
            (path, node_id)
        ),
    )
    monkeypatch.setattr(jobs, "owed_directories", lambda *_: [])
    return marked


def open_job(*files: str, **kwargs: list[tuple[str, str]]) -> int:
    """Open a job of project alpha over the given files."""
    job_id = jobs.create_job("alpha", 2000, False, 300, None)
    jobs.populate_job(graph(*files, **kwargs), job_id, "alpha", False)
    return job_id


def test_one_project_has_one_running_job(marks: list[Any]) -> None:
    """A second would describe everything the first has not reached yet."""
    job_id = open_job("a.py")
    with pytest.raises(RuntimeError):
        jobs.create_job("alpha", 2000, False, 300, None)
    assert jobs.running_job("alpha")["id"] == job_id  # type: ignore[index]
    assert jobs.create_job("beta", 2000, False, 300, None) > job_id


def test_the_owed_statements_honour_the_skip_bit(marks: list[Any]) -> None:
    """Without this the same unreadable files were queued again every job."""
    cursor = graph()
    jobs.owed_nodes(cursor, "beta", False)
    owed = [sql for sql in cursor.sent if "FROM graph_nodes AS n" in sql]
    assert len(owed) == 2
    assert all("'skip'" in sql for sql in owed)
    assert all(SKIP_SUMMARIZE in params for params in cursor.params[:1])


def test_files_are_claimed_before_entities(
    clock: list[float], marks: list[Any]
) -> None:
    """Rank orders the queue: files, then directories, then entities."""
    job_id = open_job("b.py", "a.py", entities=[("a.py::run@L3", "a.py")])
    claimed = jobs.claim_batch(job_id, 3, "t1", "w", 60, 3)
    assert [task["kind"] for task in claimed] == [
        nodetext.FILE,
        nodetext.FILE,
        nodetext.ENTITY,
    ]
    progress = jobs.job_progress(job_id)
    assert (progress["total"], progress["leased"], progress["pending"]) == (3, 3, 0)


def test_a_result_needs_the_lease_that_is_still_held(
    clock: list[float], marks: list[Any]
) -> None:
    """A worker back after its lease ran out must not overwrite the next one's."""
    job_id = open_job("a.py")
    task = jobs.claim_batch(job_id, 1, "t1", "w", 60, 3)[0]
    assert jobs.lock_leased_task(task["task_id"], "other") is None
    held = jobs.lock_leased_task(task["task_id"], "t1")
    assert held is not None and held["project"] == "alpha"
    clock[0] += 61
    jobs.reclaim_expired(job_id)
    assert jobs.lock_leased_task(task["task_id"], "t1") is None


def test_a_finished_job_closes_and_frees_its_project(
    clock: list[float], marks: list[Any]
) -> None:
    """Closing drops the task table and keeps the counts for the listing."""
    job_id = open_job("a.py")
    task = jobs.claim_batch(job_id, 1, "t1", "w", 60, 3)[0]
    jobs.finish_task(task["task_id"], None)
    assert jobs.finish_job_if_drained(graph(), job_id)
    assert jobs.running_job("alpha") is None
    assert jobs.job_row(job_id)["status"] == "done"  # type: ignore[index]
    assert jobs.job_progress(job_id)["done"] == 1
    assert jobs.job_progress(job_id)["from_model"] == 1
    assert not queue.client().exists(queue.key("sum", "job", job_id, "task"))
    assert queue.client().ttl(queue.key("sum", "job", job_id)) > 0


def test_a_task_whose_attempts_run_out_is_failed_and_marked(
    clock: list[float], marks: list[tuple[str, str | None]]
) -> None:
    """Pending or run out, a spent task is given up on rather than left owed."""
    job_id = open_job("a.py")
    for _ in range(2):
        jobs.claim_batch(job_id, 1, "t", "w", 60, 2)
        clock[0] += 61
    assert jobs.fail_spent(graph(), job_id, "alpha", 2) == 1
    assert marks == [("a.py", None)]
    assert jobs.job_progress(job_id)["failed"] == 1


def test_a_failure_is_pending_until_the_last_attempt(
    clock: list[float], marks: list[tuple[str, str | None]]
) -> None:
    """Failed is the state the node is marked at, and no earlier one."""
    job_id = open_job("a.py")
    node = jobs.Node("a.py", "file", "a.py")
    task = jobs.claim_batch(job_id, 1, "t", "w", 60, 2)[0]
    assert jobs.fail_and_mark(graph(), task["task_id"], "alpha", node, "x", 2) == (
        "pending"
    )
    assert marks == []
    task = jobs.claim_batch(job_id, 1, "t", "w", 60, 2)[0]
    assert jobs.fail_and_mark(graph(), task["task_id"], "alpha", node, "x", 2) == (
        "failed"
    )
    assert marks == [("a.py", None)]


def test_handing_back_returns_the_attempt(clock: list[float], marks: list[Any]) -> None:
    """A server that did not answer must not exhaust anyone's retries."""
    job_id = open_job("a.py")
    jobs.claim_batch(job_id, 1, "t1", "w", 60, 3)
    assert jobs.hand_back(job_id, "t1") == 1
    assert jobs.claim_batch(job_id, 1, "t2", "w", 60, 3)[0]["attempts"] == 1
    assert jobs.release_lease(job_id, "t2") == 1
    assert jobs.claim_batch(job_id, 1, "t3", "w", 60, 3)[0]["attempts"] == 2


def test_a_heartbeat_pushes_the_deadline_back(
    clock: list[float], marks: list[Any]
) -> None:
    """A slow worker that says it is alive keeps its batch."""
    job_id = open_job("a.py")
    task = jobs.claim_batch(job_id, 1, "t1", "w", 60, 3)[0]
    clock[0] += 50
    assert jobs.extend_lease(job_id, "t1", 60) == 1
    clock[0] += 50
    jobs.reclaim_expired(job_id)
    assert jobs.lock_leased_task(task["task_id"], "t1") is not None


def test_a_task_handed_out_once_is_closed_from_the_cache(
    clock: list[float], marks: list[Any]
) -> None:
    """The model is never asked about text it has already described."""
    job_id = open_job("a.py")
    task = jobs.claim_batch(job_id, 1, "t1", "w", 60, 3)[0]
    jobs.set_task_hash(task["task_id"], "d1")
    jobs.release_lease(job_id, "t1")
    settled = jobs.settle_cached(graph(cached=[("d1", "Runs a.")]), job_id, "alpha")
    assert [(tid, summary) for tid, _, summary, _ in settled] == [
        (task["task_id"], "Runs a.")
    ]
    assert jobs.job_progress(job_id)["from_cache"] == 1


def test_the_listing_pages_jobs_and_files(clock: list[float], marks: list[Any]) -> None:
    """Newest job first; a job's files in queue order."""
    first = open_job("a.py", "b.py")
    rows, total = jobs.list_jobs("alpha", None, 10, 0)
    assert (total, rows[0]["id"]) == (1, first)
    files, count = jobs.list_job_files(first, "pending", 1, 1)
    assert (count, files[0]["file_path"]) == (2, "b.py")
    assert jobs.cancel_job(first)
    assert jobs.list_jobs("alpha", "cancelled", 10, 0)[1] == 1


def test_queue_depth_sums_the_open_jobs(clock: list[float], marks: list[Any]) -> None:
    """What the dashboard shows is read from the gauges, not counted."""
    open_job("a.py", "b.py")
    depth = jobs.queue_depth()
    assert (depth["pending"], depth["total"]) == (2, 2)
    assert jobs.queue_depth("beta")["total"] == 0


def test_a_job_holds_a_window_and_tops_it_up_from_the_graph(
    clock: list[float], marks: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valkey holds a window of a project, not all of it, and never a node twice."""
    monkeypatch.setattr(jobs, "SUMMARY_JOB_WINDOW", 2)
    files = ("a.py", "b.py", "c.py")
    job_id = open_job(*files)
    assert jobs.job_progress(job_id)["pending"] == 2
    for task in jobs.claim_batch(job_id, 2, "t", "w", 60, 3):
        jobs.finish_task(task["task_id"], None)
    assert not queue.client().hlen(queue.key("sum", "job", job_id, "task"))
    assert not jobs.finish_job_if_drained(graph(*files), job_id)
    (last,) = jobs.claim_batch(job_id, 2, "t", "w", 60, 3)
    assert last["file_path"] == "c.py"
    jobs.finish_task(last["task_id"], None)
    assert jobs.finish_job_if_drained(graph(*files), job_id)
    assert jobs.job_progress(job_id)["done"] == 3


def test_a_limited_job_is_never_topped_up(clock: list[float], marks: list[Any]) -> None:
    """A job asked for N nodes is those N."""
    job_id = jobs.create_job("alpha", 2000, False, 300, None)
    assert jobs.populate_job(graph("a.py", "b.py"), job_id, "alpha", False, 1) == 1
    assert jobs.top_up(graph("a.py", "b.py"), job_id) == 0
