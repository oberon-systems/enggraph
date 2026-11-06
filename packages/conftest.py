"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest

from enggraph.core import queue


@pytest.fixture(autouse=True)
def valkey() -> Iterator[fakeredis.FakeValkey]:
    """Give every test an empty in-process Valkey, so none reaches a real one."""
    server = fakeredis.FakeValkey(decode_responses=True)
    queue.use(server)
    yield server
    queue.use(None)
