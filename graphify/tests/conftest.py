"""Shared fixtures.

`enggraph.summarizer` imports llama_cpp at module level, which is the truth
about what the indexer needs. PyPI ships that package as a source
distribution, so requiring it here would put a llama.cpp build in front of the
test suite. Every test mocks the model anyway, so an absent package is stubbed
rather than installed.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from unittest.mock import MagicMock

import diskcache
import fakeredis
import pytest

try:
    import llama_cpp  # noqa: F401
except ImportError:
    sys.modules["llama_cpp"] = MagicMock()

from enggraph import listcache, queue  # noqa: E402
from enggraph.summarizer import Summarizer  # noqa: E402


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


@pytest.fixture
def summarizer(monkeypatch: pytest.MonkeyPatch) -> Summarizer:
    """Build a Summarizer whose model is a mock and whose weights are not read."""
    monkeypatch.setattr("enggraph.summarizer.resolve_model", lambda path: path)
    monkeypatch.setattr("enggraph.summarizer.Llama", MagicMock())
    return Summarizer("/models/absent.gguf")
