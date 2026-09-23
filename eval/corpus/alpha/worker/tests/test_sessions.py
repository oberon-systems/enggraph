"""Tests for session cleanup."""

from datetime import datetime, timedelta

from worker.cleanup.sessions import purge_expired_sessions


def test_purges_only_expired() -> None:
    """Only sessions older than the ttl are purged."""
    now = datetime(2024, 1, 2, 12)
    sessions = {"old": now - timedelta(days=1), "new": now}
    assert purge_expired_sessions(sessions, now, timedelta(hours=12)) == 1
    assert list(sessions) == ["new"]
