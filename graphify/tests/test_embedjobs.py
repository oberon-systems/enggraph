"""The embedding queue in Valkey, and the coverage it is measured against.

The list of owed files is still a statement over the graph, so that part is
pinned by the SQL it sends. The queue itself runs against an in-process
Valkey (see conftest), so claims, leases and gauges are exercised for real.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from psycopg2.extensions import cursor as Cursor

from enggraph import embedjobs, queue


class Capturing:
    """A cursor that remembers every statement and answers set rows."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        """Start with nothing sent, answering `rows` to every fetch."""
        self.sql = ""
        self.sent: list[str] = []
        self.params: tuple[Any, ...] = ()
        self.rows = rows or []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Keep what would have been sent."""
        self.sql = " ".join(sql.split())
        self.sent.append(self.sql)
        self.params = params

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Answer the configured rows."""
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        """Answer the first configured row."""
        return self.rows[0] if self.rows else None


def fake(rows: list[tuple[Any, ...]] | None = None) -> Cursor:
    """Return a capturing cursor standing in for psycopg2's."""
    return cast(Cursor, Capturing(rows))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pin the queue's clock to a value the test moves by hand."""
    moment = [1000.0]
    monkeypatch.setattr(queue, "now", lambda: moment[0])
    return moment


def test_the_files_come_from_the_graph_not_from_the_hashes() -> None:
    """The graph is what coverage counts, so it is what the queue is built from."""
    cursor = fake()
    embedjobs.owed_files(cursor, "alpha", "nomic", 1500)

    assert "FROM graph_nodes AS n" in cursor.sql
    assert "LEFT JOIN file_hashes AS h" in cursor.sql
    assert "COALESCE(h.hash, '')" in cursor.sql
    assert "SELECT DISTINCT ON (n.file_path)" in cursor.sql
    assert ("alpha", "nomic", 1500) == (
        cursor.params[0],
        cursor.params[2],
        cursor.params[3],
    )


def test_a_file_with_the_embed_skip_bit_is_not_owed() -> None:
    """Given up on, it is left out of the sweep until someone retries it."""
    from enggraph.storage import SKIP_EMBED

    cursor = fake()
    embedjobs.owed_files(cursor, "eta", "nomic")
    assert "'skip'" in cursor.sql
    assert SKIP_EMBED in cursor.params


def test_a_file_queued_twice_at_one_hash_is_one_entry(clock: list[float]) -> None:
    """A sweep over a queue that has not drained adds nothing."""
    cursor = fake([("a.py", "h1"), ("b.py", "h2")])
    assert embedjobs.enqueue_project(cursor, "alpha", "nomic") == 2
    assert embedjobs.enqueue_project(cursor, "alpha", "nomic") == 0
    changed = fake([("a.py", "h3")])
    assert embedjobs.enqueue_project(changed, "alpha", "nomic") == 1
    assert embedjobs.queue_depth("alpha")["pending"] == 2


def test_a_claim_leases_the_oldest_files_and_counts_an_attempt(
    clock: list[float],
) -> None:
    """Claimed files move out of pending and carry the hash they were queued at."""
    embedjobs.enqueue_project(
        fake([("a.py", "h1"), ("b.py", "h2"), ("c.py", "h3")]),
        "alpha",
        "nomic",
    )
    taken = embedjobs.claim(["alpha"], 2, 60)
    assert [(task["file_path"], task["content_hash"]) for task in taken] == [
        ("a.py", "h1"),
        ("b.py", "h2"),
    ]
    assert all(task["attempts"] == 1 for task in taken)
    depth = embedjobs.queue_depth("alpha")
    assert (depth["pending"], depth["running"]) == (1, 2)


def test_an_expired_lease_goes_back_to_the_queue(clock: list[float]) -> None:
    """A loop that died holding files releases them by the clock."""
    embedjobs.enqueue_project(fake([("a.py", "h1")]), "alpha", "nomic")
    embedjobs.claim(["alpha"], 1, 60)
    clock[0] += 61
    assert embedjobs.queue_depth("alpha")["pending"] == 1
    again = embedjobs.claim(["alpha"], 1, 60)
    assert again[0]["attempts"] == 2


def test_an_empty_file_finished_is_not_queued_again(clock: list[float]) -> None:
    """It writes no chunk, so only the done mark keeps the sweep from repeating it."""
    owed = fake([("empty.py", "h1")])
    embedjobs.enqueue_project(owed, "alpha", "nomic")
    embedjobs.finish(embedjobs.claim(["alpha"], 1, 60)[0], empty=True)
    assert embedjobs.enqueue_project(owed, "alpha", "nomic") == 0
    assert embedjobs.done_paths("alpha") == {"empty.py"}
    assert embedjobs.queue_depth("alpha")["done"] == 1


def test_a_release_gives_the_attempt_back(clock: list[float]) -> None:
    """No server answering is nobody's fault, so it costs no retry."""
    embedjobs.enqueue_project(fake([("a.py", "h1")]), "alpha", "nomic")
    embedjobs.release(embedjobs.claim(["alpha"], 1, 60)[0])
    assert embedjobs.claim(["alpha"], 1, 60)[0]["attempts"] == 1


def test_a_file_out_of_attempts_is_failed_and_marked(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed is the state the node is marked at, and no earlier one."""
    marked: list[str] = []
    monkeypatch.setattr(
        embedjobs, "mark_skip", lambda cursor, project, path, *_: marked.append(path)
    )
    embedjobs.enqueue_project(fake([("a.py", "h1")]), "alpha", "nomic")
    task = embedjobs.claim(["alpha"], 1, 60)[0]
    assert embedjobs.fail(fake(), task, "boom", 2) == "pending"
    assert marked == []
    task = embedjobs.claim(["alpha"], 1, 60)[0]
    assert embedjobs.fail(fake(), task, "boom", 2) == "failed"
    assert marked == ["a.py"]
    assert embedjobs.queue_depth("alpha")["failed"] == 1


def test_retrying_forgives_the_failed_files_and_clears_their_marks(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry is what brings a failed file back into the next sweep."""
    monkeypatch.setattr(embedjobs, "mark_skip", lambda *_: None)
    embedjobs.enqueue_project(fake([("a.py", "h1")]), "alpha", "nomic")
    embedjobs.fail(fake(), embedjobs.claim(["alpha"], 1, 60)[0], "x", 1)
    cursor = fake()
    assert embedjobs.retry_failed(cursor, "alpha") >= 1
    assert embedjobs.queue_depth("alpha")["failed"] == 0
    assert any("& ~" in sql for sql in cursor.sent)


def test_forgetting_a_project_drops_its_queue(clock: list[float]) -> None:
    """A renamed or dropped project leaves nothing behind for the sweep to trip on."""
    embedjobs.enqueue_project(fake([("a.py", "h1")]), "alpha", "nomic")
    assert queue.projects_with_keys() == {"alpha"}
    queue.forget_project("alpha")
    assert queue.projects_with_keys() == set()


class Coverage(Capturing):
    """A cursor answering each coverage statement with its own rows."""

    def __init__(self, answers: list[list[tuple[Any, ...]]]) -> None:
        """Take one list of rows per statement, in the order they are sent."""
        super().__init__()
        self.answers = answers

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Answer the rows of the statement just sent."""
        return self.answers[len(self.sent) - 1]


def test_coverage_is_counted_for_every_project_in_one_pass() -> None:
    """Grouped by project: three statements a pass, whatever the project count."""
    from enggraph.storage import embedding_coverage

    cursor: Any = Coverage(
        [
            [("alpha", 10, 2), ("beta", 4, 0)],
            [("alpha", 3, 5, 1, ["empty.py", "owed.py"])],
            [("alpha", 2, 2)],
        ]
    )
    counts = embedding_coverage(cursor, "nomic", {"alpha": {"empty.py"}})
    assert all("GROUP BY" in sql for sql in cursor.sent)
    assert counts["alpha"] == {
        "chunks": 10,
        "summary_chunks": 2,
        "summaries": 2,
        "summaries_embedded": 2,
        "files": 4,
        "indexed_files": 5,
        "skipped": 1,
    }
    assert counts["beta"]["chunks"] == 4


def test_a_summary_counts_as_embedded_only_while_its_vector_is_current() -> None:
    """The same test summaries_owed negates, or owed and done would disagree."""
    from enggraph.storage import embedding_coverage

    cursor: Any = Coverage([[], [], []])
    embedding_coverage(cursor, "nomic", {})
    assert "e.kind = 'summary'" in cursor.sent[2]
    assert "e.content_hash = MD5(n.summary)" in cursor.sent[2]


def test_summary_coverage_leaves_skipped_nodes_out_of_the_percent() -> None:
    """Skipped nodes are counted apart, not owed and not in the total."""
    from enggraph.storage import summary_coverage

    cursor: Any = Coverage(
        [[("beta", "files", 10, 7, 1, 2), ("beta", "directories", 3, 1, 0, 0)]]
    )
    counts = summary_coverage(cursor)
    assert counts["beta"]["files"] == 10
    assert counts["beta"]["described"] == 7
    assert counts["beta"]["skipped"] == 2
    assert counts["beta"]["levels"]["directories"]["total"] == 3
    assert counts["beta"]["levels"]["entities"]["total"] == 0
    assert "NOT s.skipped" in cursor.sql
