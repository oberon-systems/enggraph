"""Remove expired login sessions."""

from __future__ import annotations

from datetime import datetime, timedelta


def expired(sessions: dict[str, datetime], now: datetime, ttl: timedelta) -> list[str]:
    """Session ids older than the ttl."""
    return [sid for sid, created in sessions.items() if now - created > ttl]


def purge_expired_sessions(
    sessions: dict[str, datetime], now: datetime, ttl: timedelta
) -> int:
    """Drop expired sessions in place and return how many went."""
    gone = expired(sessions, now, ttl)
    for sid in gone:
        del sessions[sid]
    return len(gone)
