"""What the dashboard's lamps read: the last thing each server did."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from enggraph.core import serverstate
from enggraph.core.config import FEATURE_EMBEDDING, FEATURE_SUMMARIZE
from enggraph.core.embedder import Embedder, EmbedError
from enggraph.core.llamachat import Chat, ChatError

URL = "http://alpha.example.com:8080"
# A loopback port nothing listens on refuses at once, where a real host hangs.
DEAD = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    """Start and end every test with nothing remembered."""
    serverstate.clear()
    yield
    serverstate.clear()


def test_an_address_never_dialled_is_unknown() -> None:
    """Nothing recorded reads as unknown, not as down."""
    assert serverstate.read(FEATURE_EMBEDDING, URL)["state"] == serverstate.UNKNOWN


def test_no_address_says_so() -> None:
    """An empty URL is unknown and says why."""
    state = serverstate.read(FEATURE_EMBEDDING, "")
    assert state["state"] == serverstate.UNKNOWN
    assert state["reason"] == "no server URL is set"


def test_an_outage_keeps_the_time_it_began() -> None:
    """A second failure does not move the start of the outage."""
    serverstate.record(FEATURE_EMBEDDING, URL, "does not answer")
    began = serverstate.read(FEATURE_EMBEDDING, URL)["since"]
    serverstate.record(FEATURE_EMBEDDING, URL, "does not answer")
    assert serverstate.read(FEATURE_EMBEDDING, URL)["since"] == began


def test_an_answer_clears_the_reason() -> None:
    """An answer after a failure reads as ok with no reason."""
    serverstate.record(FEATURE_EMBEDDING, URL, "does not answer")
    serverstate.record(FEATURE_EMBEDDING, URL)
    state = serverstate.read(FEATURE_EMBEDDING, URL)
    assert state["state"] == serverstate.OK
    assert state["reason"] == ""


def test_a_reason_is_cut_to_its_first_line() -> None:
    """Only the first line of a reason is kept."""
    serverstate.record(FEATURE_EMBEDDING, URL, "no answer\nstart one like this")
    assert serverstate.read(FEATURE_EMBEDDING, URL)["reason"] == "no answer"


def test_the_two_queues_are_kept_apart() -> None:
    """One address down for embedding says nothing about summarizing."""
    serverstate.record(FEATURE_EMBEDDING, URL, "does not answer")
    assert serverstate.read(FEATURE_SUMMARIZE, URL)["state"] == serverstate.UNKNOWN


def test_an_embedding_server_that_refuses_is_recorded_down() -> None:
    """The embedder records a refused connection."""
    with pytest.raises(EmbedError):
        Embedder(stored_url=DEAD).embed_one("ping")
    assert serverstate.read(FEATURE_EMBEDDING, DEAD)["state"] == serverstate.DOWN


def test_a_chat_server_that_refuses_is_recorded_down() -> None:
    """The chat client records a refused connection."""
    with pytest.raises(ChatError):
        Chat(stored_url=DEAD).props()
    assert serverstate.read(FEATURE_SUMMARIZE, DEAD)["state"] == serverstate.DOWN
