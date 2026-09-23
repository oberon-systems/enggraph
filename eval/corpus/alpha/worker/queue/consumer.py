"""Consume background jobs from the message queue."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class Queue(Protocol):
    """A message queue the worker reads from."""

    def pop(self, timeout: float) -> dict[str, str] | None:
        """Take the next message, or None after the timeout."""

    def ack(self, message_id: str) -> None:
        """Confirm a message was handled."""


def consume(
    queue: Queue, handlers: dict[str, Callable[[dict[str, str]], None]], limit: int
) -> int:
    """Dispatch up to `limit` messages to the handler named by their kind."""
    handled = 0
    while handled < limit:
        message = queue.pop(timeout=1.0)
        if message is None:
            break
        handlers[message["kind"]](message)
        queue.ack(message["id"])
        handled += 1
    return handled
