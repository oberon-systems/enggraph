"""How a background index run closes its row, and the lock that guards it."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from enggraph.api import indexjobs, listcache, scheduler
from enggraph.core import queue, trees
from enggraph.indexer import indexer


class Inline:
    """A thread that runs its target at start, so the test can wait on nothing."""

    def __init__(self, target: Callable[[], None], **kwargs: object) -> None:
        """Keep the target."""
        self._target = target

    def start(self) -> None:
        """Run it here."""
        self._target()


def test_a_failed_enqueue_leaves_the_run_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run left `running` blocks every later run of its project."""
    events: list[str] = []
    conn = MagicMock()
    conn.commit.side_effect = lambda: events.append("commit")
    conn.rollback.side_effect = lambda: events.append("rollback")
    monkeypatch.setattr(indexjobs, "get_db_connection", lambda: conn)
    monkeypatch.setattr(indexjobs.threading, "Thread", Inline)
    monkeypatch.setattr(indexjobs, "keep_lock", lambda *args: None)
    monkeypatch.setattr(indexer, "scan_and_build_graph", lambda *args: {"files": 1})
    monkeypatch.setattr(
        indexjobs, "close_job", lambda *args: events.append("close_job")
    )

    def queue_embeddings(cursor: object, project: str) -> None:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(indexjobs, "queue_embeddings", queue_embeddings)
    monkeypatch.setattr(indexjobs.listcache, "refresh", lambda *args: None)
    indexjobs.run_in_background(1, "alpha", "/srv/alpha", None, False)

    assert events == ["close_job", "commit", "commit", "rollback"]
    conn.close.assert_called_once()


def test_a_project_is_indexed_by_one_run_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock refuses a second run while the first one holds it."""
    monkeypatch.setattr(trees.Unserved, "available", lambda self: True)
    monkeypatch.setattr(indexjobs, "open_job", lambda *args: 7)
    monkeypatch.setattr(indexjobs, "job_row", lambda cursor, job_id: {"id": job_id})
    cursor = MagicMock()
    assert indexjobs.open_run(cursor, "alpha", None, False) == {"id": 7}
    with pytest.raises(RuntimeError):
        indexjobs.open_run(cursor, "alpha", None, False)
    assert indexjobs.running_job(cursor, "alpha") == {"id": 7}


def test_a_run_releases_only_its_own_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A late release of an old run must not free the lock of the next one."""
    monkeypatch.setattr(trees.Unserved, "available", lambda self: True)
    monkeypatch.setattr(indexjobs, "open_job", lambda *args: 8)
    monkeypatch.setattr(indexjobs, "job_row", lambda cursor, job_id: {"id": job_id})
    indexjobs.open_run(MagicMock(), "alpha", None, False)
    assert not indexjobs.renew_lock("alpha", 3, 0)
    assert indexjobs.renew_lock("alpha", 8, 0)
    assert indexjobs.running_job(MagicMock(), "alpha") is None


def test_a_run_is_refused_while_the_process_is_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs share one memory limit, so the cap holds across projects."""
    monkeypatch.setattr(trees.Unserved, "available", lambda self: True)
    monkeypatch.setattr(indexjobs, "open_job", lambda *args: 9)
    monkeypatch.setattr(indexjobs, "job_row", lambda cursor, job_id: {"id": job_id})
    monkeypatch.setattr(indexjobs, "INDEX_MAX_RUNNING", 1)
    monkeypatch.setattr(indexjobs, "_live_runs", 1)
    with pytest.raises(RuntimeError, match="at most 1"):
        indexjobs.open_run(MagicMock(), "beta", None, False)
    assert indexjobs.running_job(MagicMock(), "beta") is None


def test_asking_again_keeps_the_place_in_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second press neither duplicates the request nor loses its fresh flag."""
    monkeypatch.setattr(indexjobs, "_waiting", {})
    indexjobs.enqueue("alpha", "/srv/alpha", None, True)
    indexjobs.enqueue("beta", "/srv/beta", None, False)
    view = indexjobs.enqueue("alpha", "/srv/alpha", None, False)
    assert (view["status"], view["fresh"]) == ("queued", True)
    assert [one.project for one in indexjobs.waiting_runs()] == ["alpha", "beta"]
    assert indexjobs.queued("gamma") is None


def test_the_queue_starts_oldest_first_and_keeps_what_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy project waits its turn; one that can never start is dropped."""
    started: list[str] = []
    monkeypatch.setattr(indexjobs, "_waiting", {})
    monkeypatch.setattr(indexjobs, "INDEX_MAX_RUNNING", 2)
    monkeypatch.setattr(indexjobs, "_live_runs", 0)
    monkeypatch.setattr(indexjobs, "get_db_connection", MagicMock)

    def open_run(cursor: object, project: str, kind: object, fresh: bool) -> dict:
        if project == "alpha":
            raise indexjobs.Busy("job 7 is already indexing this project")
        if project == "beta":
            raise RuntimeError("beta is not served")
        return {"id": len(started) + 1}

    def run(job_id: int, project: str, *args: object) -> None:
        indexjobs.dequeue(project)
        started.append(project)
        indexjobs._count_run(1)

    monkeypatch.setattr(indexjobs, "open_run", open_run)
    monkeypatch.setattr(indexjobs, "run_in_background", run)
    for name in ("alpha", "beta", "gamma", "delta", "omega"):
        indexjobs.enqueue(name, f"/srv/{name}", None, False)
    indexjobs.start_queued()
    assert started == ["gamma", "delta"]
    assert [one.project for one in indexjobs.waiting_runs()] == ["alpha", "omega"]


@pytest.mark.parametrize("failure", ["index", "connect"])
def test_a_run_frees_its_slot_however_it_ends(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A slot held after the run is gone would stop indexing for good."""
    monkeypatch.setattr(indexjobs.threading, "Thread", Inline)
    monkeypatch.setattr(indexjobs, "keep_lock", lambda *args: None)
    monkeypatch.setattr(indexjobs, "close_job", lambda *args: None)

    def scan(*args: object) -> dict[str, int]:
        raise ValueError("unreadable tree")

    def connect() -> MagicMock:
        raise OSError("database gone")

    monkeypatch.setattr(indexer, "scan_and_build_graph", scan)
    if failure == "connect":
        monkeypatch.setattr(indexjobs, "get_db_connection", connect)
        with pytest.raises(OSError):
            indexjobs.run_in_background(1, "alpha", "/srv/alpha", None, False)
    else:
        monkeypatch.setattr(indexjobs, "get_db_connection", MagicMock)
        indexjobs.run_in_background(1, "alpha", "/srv/alpha", None, False)
    assert indexjobs.live_runs() == 0


def test_a_finished_run_rewrites_the_listed_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard lists these counts instead of counting the graph."""
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (5, 4, 2, {"file": 2}, {"contains": 3}, {}, 1)
    monkeypatch.setattr(indexjobs, "get_db_connection", lambda: conn)
    monkeypatch.setattr(indexjobs.threading, "Thread", Inline)
    monkeypatch.setattr(indexjobs, "keep_lock", lambda *args: None)
    monkeypatch.setattr(indexjobs, "close_job", lambda *args: None)
    monkeypatch.setattr(indexjobs, "queue_embeddings", lambda *args: None)
    monkeypatch.setattr(indexer, "scan_and_build_graph", lambda *args: {"files": 1})
    indexjobs.run_in_background(1, "alpha", "/srv/alpha", None, False)

    [entry] = listcache.read_all()
    assert (entry["project"], entry["nodes"], entry["edges"], entry["files"]) == (
        "alpha",
        5,
        4,
        2,
    )


def test_the_scheduler_starts_nothing_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owed project waits for a free slot instead of joining the pile."""
    started: list[str] = []
    ticker = scheduler.Scheduler()
    monkeypatch.setattr(scheduler, "get_db_connection", MagicMock)
    monkeypatch.setattr(
        ticker,
        "_plan",
        lambda cursor, now: ({}, [("alpha", "/src/alpha", "changed")]),
    )
    monkeypatch.setattr(ticker, "_watch", lambda targets: None)
    monkeypatch.setattr(
        ticker, "_begin", lambda conn, project, root, why: started.append(project)
    )
    monkeypatch.setattr(indexjobs, "INDEX_MAX_RUNNING", 1)
    monkeypatch.setattr(indexjobs, "_live_runs", 1)
    ticker.tick()
    assert started == []
    monkeypatch.setattr(indexjobs, "_live_runs", 0)
    ticker.tick()
    assert started == ["alpha"]


def test_a_refused_start_keeps_the_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """A project still indexing is run again after, not forgotten."""
    ticker = scheduler.Scheduler()
    ticker.mark("alpha")

    def refuse(*args: object, **kwargs: object) -> dict:
        raise indexjobs.Busy("job 7 is already indexing this project")

    monkeypatch.setattr(indexjobs, "open_run", refuse)
    ticker._begin(MagicMock(), "alpha", "/srv/alpha", "changed")
    assert "alpha" in ticker._dirty

    started: list[str] = []
    monkeypatch.setattr(indexjobs, "open_run", lambda *args, **kwargs: {"id": 3})
    monkeypatch.setattr(
        indexjobs, "run_in_background", lambda *args: started.append(args[1])
    )
    ticker._begin(MagicMock(), "alpha", "/srv/alpha", "changed")
    assert "alpha" not in ticker._dirty
    assert started == ["alpha"]


def test_a_resync_owes_a_run_whatever_the_ignore_spec_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full pass names no path for the spec to look at."""
    marked: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler, "readers", lambda projects: {})
    watcher = scheduler.Watcher({"alpha": None}, lambda *one: marked.append(one))
    watcher._heard("alpha", trees.RESYNC)
    assert marked == [("alpha", "")]


def test_an_agent_going_away_does_not_end_the_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One project's agent leaving must not stop hearing every other project."""
    marked: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler, "readers", lambda projects: {})
    watcher = scheduler.Watcher({"alpha": None}, lambda *one: marked.append(one))

    def gone(project: str, rel_path: str) -> bool:
        raise trees.TreeUnavailable("offline")

    monkeypatch.setattr(watcher, "selected", gone)
    watcher._heard("alpha", "a.py")
    assert marked == []


def test_a_switch_to_the_agent_rebuilds_the_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The set of projects is the same; what reads one of them is not."""
    reader = {"alpha": "/srv/alpha"}
    monkeypatch.setattr(scheduler, "readers", lambda projects: dict(reader))
    built: list[object] = []

    class Quiet(scheduler.Watcher):
        def start(self) -> None:
            built.append(self)

        def alive(self) -> bool:
            return True

    monkeypatch.setattr(scheduler, "Watcher", Quiet)
    loop = scheduler.Scheduler()
    loop._watch({"alpha": None})
    loop._watch({"alpha": None})
    assert len(built) == 1
    reader["alpha"] = "export alpha of agent agent-1"
    loop._watch({"alpha": None})
    assert len(built) == 2


def test_a_finished_run_marks_its_summaries_for_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embedding loop reads a project's owed summaries only once marked."""
    monkeypatch.setattr(indexjobs, "get_db_connection", lambda: MagicMock())
    monkeypatch.setattr(indexjobs.threading, "Thread", Inline)
    monkeypatch.setattr(indexjobs, "keep_lock", lambda *args: None)
    monkeypatch.setattr(indexjobs, "close_job", lambda *args: None)
    monkeypatch.setattr(indexjobs, "queue_embeddings", lambda *args: None)
    monkeypatch.setattr(indexjobs.listcache, "refresh", lambda *args: None)
    monkeypatch.setattr(indexer, "scan_and_build_graph", lambda *args: {"files": 1})
    indexjobs.run_in_background(1, "alpha", "/srv/alpha", None, False)

    assert queue.take_summaries_changed() == {"alpha"}
    assert queue.take_summaries_changed() == set()
