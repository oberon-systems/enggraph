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


def test_a_drain_keeps_describing_until_the_work_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One batch and a sleep made a GPU sit idle between every four files."""
    loop = SummarizeLoop()
    left = {"beta": 3}

    def push(conn: object, project: str, url: str, key: str, batch: int) -> int:
        taken = min(1, left[project])
        left[project] -= taken
        return taken

    monkeypatch.setattr(loop, "push", push)
    loop.drain(connection(), {"beta": ("http://gpu:8080", "", 1)})
    assert left["beta"] == 0


def test_a_dead_server_does_not_hold_up_another_project(
    queue: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The project on the dead address leaves the drain; the other carries on."""
    chats = {
        ("http://dead:8080", ""): FakeChat(alive=False),
        ("http://gpu:8080", ""): FakeChat(),
    }
    loop = SummarizeLoop()
    monkeypatch.setattr(loop, "chat_for", lambda url, key: chats[(url, key)])
    batches = {"beta": 2}

    def job_for(cursor: object, project: str) -> dict[str, Any] | None:
        assert project == "beta", "the dead project was asked for a job"
        return {"id": 3} if batches["beta"] else None

    def take_batch(*args: object, **kwargs: object) -> tuple[list, int]:
        batches["beta"] -= 1
        return [task()], 2000

    monkeypatch.setattr(loop, "job_for", job_for)
    monkeypatch.setattr(loop, "take_batch", take_batch)
    monkeypatch.setattr(loop, "describe", lambda *args: True)
    loop.drain(
        connection(),
        {
            "theta": ("http://dead:8080", "", 4),
            "beta": ("http://gpu:8080", "", 4),
        },
    )
    assert batches["beta"] == 0


def test_the_same_server_gets_the_same_client_every_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which is what keeps a dead server's quiet window from resetting per tick."""
    monkeypatch.setattr(summarizeloop, "Chat", lambda **_: FakeChat())
    loop = SummarizeLoop()
    assert loop.chat_for("http://gpu:8080", "") is loop.chat_for("http://gpu:8080", "")
    assert loop.chat_for("http://gpu:8080", "") is not loop.chat_for("http://a:1", "")


def test_a_batch_the_cache_answered_keeps_the_project_in_the_drain(
    queue: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing for the model in this batch is not the same as nothing left."""
    monkeypatch.setattr(summarizeloop, "Chat", lambda **_: FakeChat())
    loop = SummarizeLoop()
    monkeypatch.setattr(loop, "job_for", lambda cursor, project: {"id": 3})
    monkeypatch.setattr(loop, "take_batch", lambda *args: ([], 4))
    assert loop.push(connection(), "beta", "http://gpu:8080") == 4


def test_a_prompt_the_server_refuses_fails_that_file_only(
    queue: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 500 on one file is recorded on it; the batch and the server carry on."""

    class Refusing(FakeChat):
        def ask(self, system: str, prompt: str, max_tokens: int) -> str:
            raise summarizeloop.ChatRejected("http://gpu:8080 refused (500)")

    assert (
        SummarizeLoop().describe(connection(), Refusing(), {"id": 3}, "beta", task())
        is True
    )
    queue.fail_and_mark.assert_called_once()


def test_a_project_gets_one_new_job_per_drain(
    queue: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a skip mark did not hold, the same files must not be re-queued at once."""
    queue.running_job.return_value = None
    queue.create_job.return_value = 9
    queue.populate_job.return_value = 2
    loop = SummarizeLoop()
    loop._opened = set()
    assert loop.job_for(MagicMock(), "eta") is not None
    assert loop.job_for(MagicMock(), "eta") is None
    queue.create_job.assert_called_once()


def test_a_file_with_no_text_fails_with_the_reason_and_is_not_skipped(
    queue: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty file gets its attempts and then its mark, not an endless skip."""
    queue.settle_cached.return_value = []
    queue.claim_batch.return_value = [
        {"task_id": 7, "file_path": "pkg/tests/__init__.py", "content_hash": ""}
    ]
    queue.read_task_content.return_value = {7: ("", "the file is empty")}
    monkeypatch.setattr("enggraph.workerapi.apply_summary", MagicMock())
    ready, settled = SummarizeLoop().take_batch(
        MagicMock(), {"id": 3, "input_chars": 2000}, "beta", "token", 4
    )
    assert (ready, settled) == ([], 1)
    args = queue.fail_and_mark.call_args[0]
    assert args[3] == "pkg/tests/__init__.py"
    assert args[4] == "the file is empty"
    queue.skip_tasks.assert_not_called()
