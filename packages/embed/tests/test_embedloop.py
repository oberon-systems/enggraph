"""When the embedding loop enqueues, and which projects a dead server stops."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from enggraph.core import queue
from enggraph.embed import embedloop
from enggraph.embed.chunks import Chunk
from enggraph.embed.embedloop import EmbedLoop, Target, embed_text

TARGET = Target("http://gpu:8085", "", 8, 1500, 5)


@pytest.fixture
def loop(monkeypatch: pytest.MonkeyPatch) -> EmbedLoop:
    """Build a loop whose database, switches and drain are stood in for."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    monkeypatch.setattr(embedloop, "get_db_connection", lambda: conn)
    pace = MagicMock(tick_seconds=10, budget_seconds=60)
    monkeypatch.setattr(embedloop.features, "resolve", lambda *args: pace)
    running = EmbedLoop()
    running._swept = time.monotonic()
    monkeypatch.setattr(running, "_drain", lambda conn, enabled: None)
    alive = MagicMock()
    alive.available.return_value = True
    monkeypatch.setattr(running, "embedder_for", lambda url, key: alive)
    return running


def test_a_project_switched_on_is_enqueued_without_waiting_for_the_sweep(
    loop: EmbedLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five minutes of an empty queue read as a queue that had stopped."""
    swept: list[set[str]] = []
    enabled = {"beta": TARGET}
    monkeypatch.setattr(loop, "_enabled", lambda cursor: dict(enabled))
    monkeypatch.setattr(
        loop, "_sweep", lambda conn, projects: swept.append(set(projects))
    )
    loop.tick()
    enabled["eta"] = TARGET
    loop.tick()
    loop.tick()
    assert swept == [{"beta"}, {"eta"}]


def test_a_project_whose_server_is_down_is_not_swept(
    loop: EmbedLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking what a project owes scans its chunks; nobody could take the work."""
    swept: list[set[str]] = []
    dead = MagicMock()
    dead.available.return_value = False
    monkeypatch.setattr(loop, "embedder_for", lambda url, key: dead)
    monkeypatch.setattr(loop, "_enabled", lambda cursor: {"beta": TARGET})
    monkeypatch.setattr(
        loop, "_sweep", lambda conn, projects: swept.append(set(projects))
    )
    loop._swept = 0.0
    assert loop.tick() is False
    assert swept == []


def test_a_project_that_cannot_be_queued_does_not_stop_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing project must not keep the ones after it from being queued."""
    queued: list[str] = []

    def enqueue(cursor: object, project: str, model: str, chunk_chars: int) -> int:
        if project == "alpha":
            raise RuntimeError("enqueue failed")
        queued.append(project)
        return 1

    monkeypatch.setattr(embedloop.embedjobs, "enqueue_project", enqueue)
    conn = MagicMock()
    EmbedLoop()._sweep(conn, {"alpha": TARGET, "beta": TARGET, "gamma": TARGET})
    assert queued == ["beta", "gamma"]
    conn.rollback.assert_called_once()


def test_a_dead_server_leaves_the_drain_and_the_other_carries_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One server going away must not stall projects pointed elsewhere."""
    loop = EmbedLoop()
    dead = MagicMock(available=MagicMock(return_value=False))
    alive = MagicMock(available=MagicMock(return_value=True))
    monkeypatch.setattr(
        loop,
        "embedder_for",
        lambda url, key: dead if url == "http://dead:8085" else alive,
    )
    claims = {
        "beta": [
            [{"id": 1, "file_path": "a.py"}],
            [{"id": 2, "file_path": "b.py"}],
            [],
        ]
    }

    def claim(projects: list[str], limit: int, lease: int) -> list:
        assert projects == ["beta"], "the dead project's files were claimed"
        return claims["beta"].pop(0)

    monkeypatch.setattr(embedloop.embedjobs, "claim", claim)
    embedded: list[int] = []
    monkeypatch.setattr(
        loop,
        "embed_file",
        lambda conn, embedder, task, target: embedded.append(task["id"]) or True,
    )
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    loop._drain(
        conn,
        {
            "theta": Target("http://dead:8085", "", 8, 1500, 5),
            "beta": TARGET,
        },
    )
    assert embedded == [1, 2]


def test_summaries_are_embedded_while_files_are_still_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long file queue must not hold every summary back until it drains."""
    loop = EmbedLoop()
    alive = MagicMock(available=MagicMock(return_value=True))
    monkeypatch.setattr(loop, "embedder_for", lambda url, key: alive)
    claims = [[{"id": 1, "file_path": "a.py"}], [{"id": 2, "file_path": "b.py"}], []]
    monkeypatch.setattr(embedloop.embedjobs, "claim", lambda *_: claims.pop(0))
    events: list[str] = []
    monkeypatch.setattr(
        loop,
        "embed_file",
        lambda conn, embedder, task, target: events.append(task["file_path"]) or True,
    )
    owed = [2, 1, 0]

    def summaries(conn: object, embedder: object, project: str, batch: int) -> int:
        events.append("summaries")
        return owed.pop(0) if owed else 0

    monkeypatch.setattr(loop, "embed_summaries", summaries)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    loop._drain(conn, {"beta": TARGET})
    assert events == ["a.py", "summaries", "b.py", "summaries", "summaries"]


def test_the_model_reads_the_path_and_entity_before_the_code() -> None:
    """An uncommented body is embedded with the words that name it."""
    piece = Chunk(0, 3, 5, "return x", entity="compute()")
    assert embed_text("src/a.py", piece) == "src/a.py\ncompute()\nreturn x"
    bare = Chunk(0, 1, 1, "x = 1")
    assert embed_text("src/a.py", bare) == "src/a.py\nx = 1"


class Owed:
    """Stand in for summaries_owed: answer pages and record where each began."""

    def __init__(self, pages: list[list[tuple[str, str, int]]]) -> None:
        """Keep the pages to answer with, in order."""
        self.pages = pages
        self.after: list[str] = []

    def __call__(
        self, cursor: object, project: str, model: str, limit: int, after: str = ""
    ) -> list[tuple[str, str, int]]:
        """Answer with the next page."""
        self.after.append(after)
        return self.pages.pop(0) if self.pages else []


def owed_rows(*ids: str) -> list[tuple[str, str, int]]:
    """Build the rows summaries_owed answers with."""
    return [(node_id, "A summary.", 0) for node_id in ids]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pin the loop's clock to a value the test moves."""
    now = [1000.0]
    monkeypatch.setattr(embedloop.time, "monotonic", lambda: now[0])
    return now


def drain_summaries(loop: EmbedLoop, project: str = "alpha") -> list[str]:
    """Take batches until the buffer and the pass give nothing more."""
    conn = MagicMock()
    taken: list[str] = []
    while batch := loop._next_summaries(conn, project, 2):
        taken.extend(node_id for node_id, _, _ in batch)
    return taken


def test_a_pass_continues_from_the_last_id_and_ends_on_a_short_page(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """Restarting from the first id rescanned every summary the pass had done."""
    monkeypatch.setattr(embedloop, "SUMMARY_PREFETCH", 2)
    owed = Owed([owed_rows("a", "b"), owed_rows("c")])
    monkeypatch.setattr(embedloop, "summaries_owed", owed)
    loop = EmbedLoop()
    assert drain_summaries(loop) == ["a", "b", "c"]
    assert owed.after == ["", "b"]


def test_a_project_that_owes_nothing_is_not_asked_again_unmarked(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """Asking every project each minute kept the database scanning for good."""
    owed = Owed([])
    monkeypatch.setattr(embedloop, "summaries_owed", owed)
    loop = EmbedLoop()
    drain_summaries(loop)
    clock[0] += embedloop.SUMMARY_RECHECK_SECONDS - 1
    drain_summaries(loop)
    assert owed.after == [""]
    clock[0] += 1
    drain_summaries(loop)
    assert owed.after == ["", ""]


def test_a_mark_starts_a_pass_once_the_gap_has_passed(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """The summarize loop marks a project with every summary it writes."""
    owed = Owed([[], owed_rows("d")])
    monkeypatch.setattr(embedloop, "summaries_owed", owed)
    loop = EmbedLoop()
    drain_summaries(loop)
    queue.summaries_changed("alpha")
    loop._take_marks()
    assert drain_summaries(loop) == []
    clock[0] += embedloop.SUMMARY_PASS_GAP_SECONDS
    assert drain_summaries(loop) == ["d"]
    assert owed.after == ["", ""]
    clock[0] += embedloop.SUMMARY_PASS_GAP_SECONDS
    drain_summaries(loop)
    assert owed.after == ["", ""]
