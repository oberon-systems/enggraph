"""How a background index run closes its row, and the lock that guards it."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from enggraph import indexer, indexjobs


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
    indexjobs.run_in_background(1, "alpha", "/code/alpha", None, False)

    assert events == ["close_job", "commit", "rollback"]
    conn.close.assert_called_once()


def test_a_project_is_indexed_by_one_run_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock refuses a second run while the first one holds it."""
    monkeypatch.setattr(indexjobs, "is_mounted", lambda mount: True)
    monkeypatch.setattr(indexjobs, "open_job", lambda *args: 7)
    monkeypatch.setattr(indexjobs, "job_row", lambda cursor, job_id: {"id": job_id})
    cursor = MagicMock()
    assert indexjobs.open_run(cursor, "alpha", None, False) == {"id": 7}
    with pytest.raises(RuntimeError):
        indexjobs.open_run(cursor, "alpha", None, False)
    assert indexjobs.running_job(cursor, "alpha") == {"id": 7}


def test_a_run_releases_only_its_own_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A late release of an old run must not free the lock of the next one."""
    monkeypatch.setattr(indexjobs, "is_mounted", lambda mount: True)
    monkeypatch.setattr(indexjobs, "open_job", lambda *args: 8)
    monkeypatch.setattr(indexjobs, "job_row", lambda cursor, job_id: {"id": job_id})
    indexjobs.open_run(MagicMock(), "alpha", None, False)
    assert not indexjobs.renew_lock("alpha", 3, 0)
    assert indexjobs.renew_lock("alpha", 8, 0)
    assert indexjobs.running_job(MagicMock(), "alpha") is None
