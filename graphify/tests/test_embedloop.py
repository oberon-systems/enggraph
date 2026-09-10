"""When the embedding loop enqueues, and which projects a dead server stops."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from enggraph import embedloop
from enggraph.embedloop import EmbedLoop, Target

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

    def claim(cursor: object, projects: list[str], limit: int, lease: int) -> list:
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
