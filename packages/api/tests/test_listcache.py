"""The listing cache: what an index run writes and what the dashboard reads."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from enggraph.api import listcache


def counting_cursor(nodes: int, edges: int, files: int) -> MagicMock:
    """Build a cursor answering the one count query with these numbers."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (nodes, edges, files, {}, {}, {}, 0)
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
    cursor.fetchone.return_value = (7, 6, 5, {}, {}, {}, 0)
    monkeypatch.setattr(listcache, "get_db_connection", lambda: conn)
    assert listcache.warm() == 1
    counted = {entry["project"]: entry["nodes"] for entry in listcache.read_all()}
    assert counted == {"alpha": 1, "beta": 7}


def test_an_entry_carries_the_breakdowns_the_project_page_reads() -> None:
    """Types, relations and extensions come back as numbers by name."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (
        3,
        2,
        1,
        {"file": 1, "function": 2},
        {"contains": 2},
        {".go": 1},
        1,
    )
    entry = listcache.refresh(cursor, "alpha")
    assert entry["types"] == {"file": 1, "function": 2}
    assert entry["relations"] == {"contains": 2}
    assert entry["extensions"] == {".go": 1}
    assert entry["hashed_files"] == 1


def test_warm_recounts_an_entry_without_breakdowns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry an older image wrote is completed once on start."""
    listcache.cache().set("alpha", {"project": "alpha", "nodes": 1})
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("alpha",)]
    cursor.fetchone.return_value = (7, 6, 5, {}, {}, {}, 0)
    monkeypatch.setattr(listcache, "get_db_connection", lambda: conn)
    assert listcache.warm() == 1
    assert listcache.complete(listcache.read_all()[0])
