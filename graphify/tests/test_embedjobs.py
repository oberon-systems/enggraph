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

import pytest

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


def test_two_file_nodes_of_one_path_are_one_task() -> None:
    """Otherwise ON CONFLICT touches one task twice and the whole enqueue fails."""
    cursor = Capturing()
    embedjobs.enqueue_project(cursor, "alpha", "nomic", 1500)

    assert "SELECT DISTINCT ON (n.file_path)" in cursor.sql
    assert "ORDER BY n.file_path ON CONFLICT (project, file_path)" in cursor.sql


def test_a_project_is_asked_for_by_name_and_by_model() -> None:
    """Both narrow the work: another model's rows are stale however fresh."""
    cursor = Capturing()
    embedjobs.enqueue_project(cursor, "alpha", "nomic", 1500)

    assert "alpha" in cursor.params
    assert "nomic" in cursor.params
    assert 1500 in cursor.params


class Counting(Capturing):
    """A cursor that also answers the one row a count returns."""

    def fetchone(self) -> tuple[int, int, int, int]:
        """Zeros: what is under test is the statement, not the answer."""
        return (0, 0, 0, 0)


def test_an_empty_file_done_with_no_chunks_counts_as_embedded() -> None:
    """Such a file writes no row, so a done task has to count, or 97% is final."""
    from enggraph.storage import embedding_coverage

    cursor = Counting()
    embedding_coverage(cursor, "beta")  # type: ignore[arg-type]
    assert "FROM embed_tasks" in cursor.sql
    assert "t.status = 'done'" in cursor.sql
    assert "FROM graph_nodes AS n" in cursor.sql


def test_a_file_with_the_embed_skip_bit_is_not_queued_nor_counted() -> None:
    """Given up on, it is left out of the sweep and out of the percent."""
    from enggraph.storage import SKIP_EMBED, embedding_coverage

    cursor = Capturing()
    embedjobs.enqueue_project(cursor, "eta", "nomic", 1500)
    assert "'skip'" in cursor.sql
    assert SKIP_EMBED in cursor.params
    counting = Counting()
    counts = embedding_coverage(counting, "eta")  # type: ignore[arg-type]
    assert "NOT s.skipped" in counting.sql
    assert counts["skipped"] == 0


class Answering(Capturing):
    """A cursor that remembers every statement and answers one row of state."""

    def __init__(self, row: tuple[Any, ...] = ("failed",)) -> None:
        """Start with nothing sent, and the row a RETURNING would give."""
        super().__init__()
        self.sent: list[str] = []
        self.row = row

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Keep every statement, in order."""
        super().execute(sql, params)
        self.sent.append(self.sql)

    def fetchone(self) -> tuple[Any, ...]:
        """Return the one row the statement would have returned."""
        return self.row


def test_a_file_with_the_summarize_skip_bit_is_not_owed_to_the_next_job() -> None:
    """Without this the same unreadable files were re-queued every tick."""
    from enggraph import jobs
    from enggraph.storage import SKIP_SUMMARIZE

    cursor = Capturing()
    jobs.populate_job(cursor, 1, "beta", False)  # type: ignore[arg-type]
    assert "'skip'" in cursor.sql
    assert cursor.params[3] == SKIP_SUMMARIZE


def test_a_task_still_owed_with_its_attempts_spent_is_failed() -> None:
    """Pending or run out, a spent task is given up on rather than left owed."""
    from enggraph import jobs

    cursor = Capturing()
    jobs.fail_spent(cursor, 1, "alpha", 3)  # type: ignore[arg-type]
    assert "SET state = 'failed'" in cursor.sql
    assert "attempts >= %s" in cursor.sql
    assert "state = 'leased' AND lease_expires_at < NOW()" in cursor.sql
    assert cursor.params == (1, 3)


def test_retrying_a_project_clears_its_marks() -> None:
    """Retry is what brings a failed file back into the next job."""
    from enggraph import jobs

    cursor = Answering()
    jobs.retry_failed(cursor, "beta")  # type: ignore[arg-type]
    assert any("& ~" in sql for sql in cursor.sent)


def test_a_task_out_of_attempts_marks_its_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed is the state the node is marked at, and no earlier one."""
    from enggraph import jobs

    marked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jobs,
        "mark_skip",
        lambda cursor, project, path, bit, reason: marked.append((path, reason)),
    )
    assert jobs.fail_and_mark(Answering(("pending",)), 1, "p", "a.py", "x", 3) == (
        "pending"
    )
    assert marked == []
    assert jobs.fail_and_mark(Answering(("failed",)), 1, "p", "a.py", "x", 3) == (
        "failed"
    )
    assert marked == [("a.py", "x")]


def test_summary_coverage_leaves_skipped_files_out_of_the_percent() -> None:
    """Skipped files are counted apart, not owed and not in the total."""
    from enggraph.storage import summary_coverage

    cursor = Answering((10, 7, 1, 2))
    counts = summary_coverage(cursor, "beta")  # type: ignore[arg-type]
    assert counts == {"files": 10, "described": 7, "manual": 1, "skipped": 2}
    assert "NOT s.skipped" in cursor.sql
