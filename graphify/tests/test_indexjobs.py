"""How a background index run closes its row."""

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
