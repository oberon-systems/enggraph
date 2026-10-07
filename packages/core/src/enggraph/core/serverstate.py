"""The last thing each server a queue dials did, for the dashboard's lamps.

Kept in Valkey, because the queues and the API are separate processes.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import valkey

from enggraph.core import queue

LOG = logging.getLogger(__name__)

OK = "ok"
DOWN = "down"
UNKNOWN = "unknown"

# One hash per kind, address -> outcome, so the API reads what a queue running
# in another process dialled. Evicted or unreachable reads as unknown.
SCOPE = "server"


def short(reason: str, limit: int = 200) -> str:
    """Return the first line of a reason, which is what fits a tooltip."""
    lines = reason.strip().splitlines()
    return lines[0][:limit] if lines else ""


def _stored(kind: str, url: str) -> dict[str, Any] | None:
    try:
        raw = queue.client().hget(queue.key(SCOPE, kind), url)
    except valkey.exceptions.ValkeyError:
        LOG.debug("The server state of %s could not be read", url, exc_info=True)
        return None
    if not raw:
        return None
    return json.loads(raw)


def record(kind: str, url: str, reason: str = "") -> None:
    """Remember one outcome: no reason is an answer, a reason is a failure."""
    if not url:
        return
    now = datetime.now(UTC).isoformat()
    state = DOWN if reason else OK
    previous = _stored(kind, url)
    since = previous["since"] if previous and previous["state"] == state else now
    outcome = {
        "url": url,
        "state": state,
        "reason": short(reason),
        "since": since,
        "checked_at": now,
    }
    try:
        queue.client().hset(queue.key(SCOPE, kind), url, json.dumps(outcome))
    except valkey.exceptions.ValkeyError:
        LOG.debug("The server state of %s could not be kept", url, exc_info=True)


def read(kind: str, url: str) -> dict[str, Any]:
    """Return what one address last did, or why nothing is known about it."""
    if not url:
        return _unknown("", "no server URL is set")
    found = _stored(kind, url)
    if found is None:
        return _unknown(url, "not dialled since the service started")
    return found


def clear() -> None:
    """Forget every outcome."""
    for name in queue.scan(queue.key(SCOPE, "*")):
        queue.client().delete(name)


def _unknown(url: str, reason: str) -> dict[str, Any]:
    """Describe an address nothing is known about."""
    return {
        "url": url,
        "state": UNKNOWN,
        "reason": reason,
        "since": None,
        "checked_at": None,
    }
