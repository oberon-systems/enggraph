"""The listing cache: what an index run writes and what the dashboard reads."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from enggraph import listcache


def counting_cursor(nodes: int, edges: int, files: int) -> MagicMock:
    """Build a cursor answering the one count query with these numbers."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (nodes, edges, files)
    return cursor


def test_a_refresh_replaces_the_entry() -> None:
    """The listing shows the last run's counts, not the first one's."""
    listcache.refresh(counting_cursor(10, 9, 3), "alpha")
    listcache.refresh(counting_cursor(12, 11, 4), "alpha")
    [entry] = listcache.read_all()
    assert (entry["nodes"], entry["edges"], entry["files"]) == (12, 11, 4)


def test_a_rename_moves_the_entry() -> None:
    """A renamed project keeps its counts under the name it goes by now."""
    listcache.refresh(counting_cursor(1, 2, 3), "alpha")
    listcache.rename("alpha", "beta")
    assert [entry["project"] for entry in listcache.read_all()] == ["beta"]


def test_warm_counts_only_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A warm start must not recount the whole graph over a full cache."""
    listcache.refresh(counting_cursor(1, 1, 1), "alpha")
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("alpha",), ("beta",)]
    cursor.fetchone.return_value = (7, 6, 5)
    monkeypatch.setattr(listcache, "get_db_connection", lambda: conn)
    assert listcache.warm() == 1
    counted = {entry["project"]: entry["nodes"] for entry in listcache.read_all()}
    assert counted == {"alpha": 1, "beta": 7}
