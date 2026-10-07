"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import diskcache
import fakeredis
import pytest

from enggraph.api import listcache
from enggraph.core import queue


@pytest.fixture(autouse=True)
def valkey() -> Iterator[fakeredis.FakeValkey]:
    """Give every test an empty in-process Valkey, so none reaches a real one."""
    server = fakeredis.FakeValkey(decode_responses=True)
    queue.use(server)
    yield server
    queue.use(None)


@pytest.fixture(autouse=True)
def list_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    """Give every test an empty listing cache of its own, never the service's."""
    store = diskcache.Cache(str(tmp_path_factory.mktemp("listcache")))
    listcache.use(store)
    yield store
    listcache.use(None)
    store.close()
