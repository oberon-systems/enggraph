"""What the embedding queue is filled from.

There is no database in this suite, so the statement itself is what is pinned:
which table the list of files comes from. That is the defect this test exists
for - the queue was built from `file_hashes` while the coverage was measured
against `graph_nodes`, so a file the indexer recorded no hash for could never
be queued, and the percentage stopped at whatever fraction happened to have
one.
"""

from __future__ import annotations

from typing import Any

from enggraph import embedjobs


class Capturing:
    """A cursor that remembers the statement and answers nothing."""

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sql = ""
        self.params: tuple[Any, ...] = ()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Keep what would have been sent."""
        self.sql = " ".join(sql.split())
        self.params = params

    def fetchall(self) -> list[tuple[Any, ...]]:
        """No rows: what is under test is the statement, not the answer."""
        return []


def test_the_files_come_from_the_graph_not_from_the_hashes() -> None:
    """The graph is what coverage counts, so it is what the queue is built from."""
    cursor = Capturing()
    embedjobs.enqueue_project(cursor, "alpha", "nomic", 1500)

    assert "FROM graph_nodes AS n" in cursor.sql
    assert "LEFT JOIN file_hashes AS h" in cursor.sql
    # A file with no hash row is queued with an empty one rather than skipped.
    assert "COALESCE(h.hash, '')" in cursor.sql
    assert "n.type = 'file'" in cursor.sql


def test_a_project_is_asked_for_by_name_and_by_model() -> None:
    """Both narrow the work: another model's rows are stale however fresh."""
    cursor = Capturing()
    embedjobs.enqueue_project(cursor, "alpha", "nomic", 1500)

    assert "alpha" in cursor.params
    assert "nomic" in cursor.params
    assert 1500 in cursor.params
