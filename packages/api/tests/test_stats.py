"""Which projects the stats loop recounts, and when it counts them all."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from enggraph.api import stats
from enggraph.core import queue, storage


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[set[str] | None]:
    """Record what each refresh was asked to count instead of counting it."""
    calls: list[set[str] | None] = []

    def refresh(projects: set[str] | None = None) -> int:
        calls.append(None if projects is None else set(projects))
        return 0

    monkeypatch.setattr(stats, "refresh", refresh)
    return calls


def test_a_changed_project_is_counted_twice_and_the_rest_never(
    counted: list[set[str] | None],
) -> None:
    """The second count sees a write committed after the first one read."""
    loop = stats.StatsLoop(every=60, full=3600)
    loop.tick()
    queue.recount("alpha")
    loop.tick()
    loop.tick()
    loop.tick()
    assert counted == [None, {"alpha"}, {"alpha"}, set()]


def test_every_project_is_counted_when_asked_or_when_due(
    counted: list[set[str] | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full pass runs on the first tick, on request and once it is due."""
    clock = [1000.0]
    monkeypatch.setattr(stats.time, "monotonic", lambda: clock[0])
    loop = stats.StatsLoop(every=60, full=3600)
    loop.tick()
    queue.recount(None)
    loop.tick()
    clock[0] += 3600
    loop.tick()
    assert counted == [None, None, None]


def test_a_failed_count_keeps_what_it_was_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marks are taken from Valkey before counting, so a failure must keep them."""
    calls: list[set[str] | None] = []

    def refresh(projects: set[str] | None = None) -> int:
        calls.append(projects)
        if projects == {"alpha"} and len(calls) == 2:
            raise OSError("database gone")
        return 0

    monkeypatch.setattr(stats, "refresh", refresh)
    loop = stats.StatsLoop(every=60, full=3600)
    loop.tick()
    queue.recount("alpha")
    with pytest.raises(OSError):
        loop.tick()
    loop.tick()
    assert calls[1:] == [{"alpha"}, {"alpha"}]


def test_a_count_of_nothing_reads_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quiet minute costs one Valkey write, not a pass over the graph."""
    refused = MagicMock(side_effect=AssertionError)
    monkeypatch.setattr(stats, "get_db_connection", refused)
    assert stats.refresh(set()) == 0
    assert stats.refreshed_at() is not None


def test_a_written_vector_asks_for_a_recount() -> None:
    """The embed worker is what moves the gauges most, so it must mark them."""
    storage.replace_summary_embedding(
        MagicMock(), "alpha", "src/a.py", "A module.", "model", 1, [0.0]
    )
    assert queue.owed_recounts() == ({"alpha"}, False)
    assert queue.owed_recounts() == (set(), False)
