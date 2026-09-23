"""Worker entry point: wire the queue handlers."""

from __future__ import annotations

from datetime import datetime, timedelta

from worker.cleanup.sessions import purge_expired_sessions
from worker.queue.consumer import Queue, consume


def run(queue: Queue, sessions: dict[str, datetime]) -> int:
    """Drain up to 100 queue messages."""
    handlers = {
        "purge_sessions": lambda _message: purge_expired_sessions(
            sessions, datetime.now(), timedelta(hours=12)
        ),
    }
    return consume(queue, handlers, limit=100)
