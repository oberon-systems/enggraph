"""Pushing the summary queue at a server, and what happens when it stops.

The queue functions are monkeypatched at their `enggraph.summarizeloop`
binding and the connection is a mock, the way test_workerapi fakes a database.
What is worth pinning here is not the SQL - `jobs` owns that - but the two
outcomes that differ from the pull path: a file described end to end, and a
server that goes away mid-batch costing the files nothing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from enggraph import summarizeloop
from enggraph.llamachat import ChatError
from enggraph.summarizeloop import SummarizeLoop


class FakeChat:
    """A server that answers, or one that has gone away."""

    def __init__(self, reply: str = "Reads the queue.", alive: bool = True) -> None:
        """Take what it answers, and whether it answers at all."""
        self.reply = reply
        self.alive = alive
        self.urls = ["http://gpu:8080"]
        self.chosen = "http://gpu:8080" if alive else None
        self.asked: list[str] = []

    def available(self) -> bool:
        """Whether this server is up, as the loop asks once a tick."""
        return self.alive

    def ask(self, system: str, prompt: str, max_tokens: int) -> str:
        """Answer, or refuse the way an unreachable address does."""
        if not self.alive:
            raise ChatError("no llama.cpp server answered at http://gpu:8080")
        self.asked.append(prompt)
        return self.reply


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stand in for `jobs`, so the loop is tested without a database."""
    fake = MagicMock()
    fake.NO_FILE = "not on the mount, re-index the project"
    monkeypatch.setattr(summarizeloop, "jobs", fake)
    return fake


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stand in for the one thing that writes a node."""
    fake = MagicMock(return_value=(True, None))
    monkeypatch.setattr("enggraph.workerapi.apply_summary", fake)
    return fake


def connection() -> MagicMock:
    """Return a connection whose cursor is a context manager, as psycopg2's is."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    return conn


def task(**over: object) -> dict[str, Any]:
    """One claimed task, as `_claim` hands it on."""
    return {
        "task_id": 7,
        "file_path": "src/queue.py",
        "digest": "abc123",
        "text": "def claim(): ...",
        "attempts": 1,
        **over,
    }


def test_a_file_is_described_cached_and_closed(
    queue: MagicMock, applied: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three writes a described file owes: the cache, the node, the task."""
    cached = MagicMock()
    monkeypatch.setattr(summarizeloop, "put_cached_summary", cached)
    chat = FakeChat()
    conn = connection()

    assert SummarizeLoop().describe(conn, chat, {"id": 3}, "alpha", task()) is True
    assert chat.asked[0].startswith("File: src/queue.py")
    cached.assert_called_once()
    assert cached.call_args[0][2] == "abc123"
    applied.assert_called_once()
    assert applied.call_args[0][2] == "src/queue.py"
    queue.finish_task.assert_called_once()


def test_an_answer_that_says_nothing_still_closes_its_task(
    queue: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused answer is the answer: the file keeps its head-of-file line."""
    monkeypatch.setattr(summarizeloop, "put_cached_summary", MagicMock())
    monkeypatch.setattr(
        "enggraph.workerapi.apply_summary",
        MagicMock(return_value=(False, "says nothing the file name does not")),
    )
    conn = connection()

    loop = SummarizeLoop()
    assert loop.describe(conn, FakeChat(), {"id": 3}, "alpha", task()) is True
    note = queue.finish_task.call_args[0][2]
    assert note == "says nothing the file name does not"


def test_a_server_that_went_away_is_not_the_file_s_fault(
    queue: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is written and nothing is failed - the caller hands the batch back."""
    monkeypatch.setattr(summarizeloop, "put_cached_summary", MagicMock())
    conn = connection()

    loop = SummarizeLoop()
    dead = FakeChat(alive=False)
    assert loop.describe(conn, dead, {"id": 3}, "alpha", task()) is False
    queue.finish_task.assert_not_called()
    queue.fail_task.assert_not_called()


def test_the_batch_goes_back_with_its_attempt_when_the_server_dies(
    queue: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hand_back`, not `release_lease`: nothing was tried, so nothing was spent.

    The server answered when the tick began and had gone by the time the
    batch was being described, which is the case the lease is held through.
    """
    monkeypatch.setattr(summarizeloop, "Chat", lambda **_: FakeChat())
    loop = SummarizeLoop()
    monkeypatch.setattr(loop, "job_for", lambda cursor, project: {"id": 3})
    monkeypatch.setattr(
        loop,
        "take_batch",
        lambda cursor, job, project, token, batch=4: ([task()], 2000),
    )
    monkeypatch.setattr(loop, "describe", lambda *args: False)
    conn = connection()

    loop.push(conn, "alpha", "http://gpu:8080")
    queue.hand_back.assert_called_once()
    queue.release_lease.assert_not_called()


def test_a_project_with_no_address_is_not_pushed_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch alone is not an invitation: there has to be somewhere to ask."""
    monkeypatch.setattr("enggraph.llamachat.SUMMARIZE_SERVER_URL", "")
    monkeypatch.setattr(
        summarizeloop, "list_mountable_projects", lambda cursor: [("alpha", "/x")]
    )
    monkeypatch.setattr(
        summarizeloop.features,
        "resolve",
        lambda cursor, project, feature: summarizeloop.features.Feature(
            name=feature,
            allowed=True,
            enabled=True,
            server_url="",
            server_key="",
            key_saved_at="",
            batch=4,
            tick_seconds=30,
            budget_seconds=60,
            origins={},
        ),
    )
    assert SummarizeLoop().targets(MagicMock()) == {}
