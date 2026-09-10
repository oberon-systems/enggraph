"""Turning text into vectors through whichever server answers.

A real HTTP server on a loopback port rather than a mocked urlopen: what is
worth pinning is the fallback from an address that refuses a connection, and a
mock cannot refuse one.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from enggraph import embedder as embedder_module
from enggraph.embedder import (
    Embedder,
    EmbedError,
    EmbedTimeout,
    candidates,
    read_vectors,
)


class Handler(BaseHTTPRequestHandler):
    """Answer /v1/embeddings the way llama-server does."""

    def do_POST(self) -> None:  # noqa: N802 - the base class names it
        """Return one vector per input, or the failure the test asked for."""
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        served = self.server  # type: ignore[attr-defined]
        served.seen.append(body)
        served.headers_seen.append(dict(self.headers))
        if served.status != 200:
            payload = json.dumps(
                {
                    "error": {
                        "code": served.status,
                        "message": served.message,
                        "type": "not_supported_error",
                    }
                }
            ).encode("utf-8")
            self.send_response(served.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        rows = [
            {"embedding": [0.5] * served.width, "index": index}
            for index, _ in enumerate(body.get("input", []))
        ]
        payload = json.dumps({"data": rows}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:
        """Keep the suite's output to the suite's own."""


@pytest.fixture
def server() -> Iterator[HTTPServer]:
    """One embedding server on a port the kernel picks."""
    running = HTTPServer(("127.0.0.1", 0), Handler)
    running.seen = []  # type: ignore[attr-defined]
    running.headers_seen = []  # type: ignore[attr-defined]
    running.status = 200  # type: ignore[attr-defined]
    running.message = "This server does not support embeddings"  # type: ignore[attr-defined]
    running.width = 4  # type: ignore[attr-defined]
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    yield running
    running.shutdown()
    running.server_close()


def url_of(server: HTTPServer) -> str:
    """Return the base address of a running server."""
    return f"http://127.0.0.1:{server.server_port}"


# An address nothing listens on. Port 1 is privileged and unbound, so a
# connection there is refused rather than left hanging.
DEAD = "http://127.0.0.1:1"


@pytest.fixture(autouse=True)
def _no_environment_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's own EMBED_* variables out of these tests.

    The width is narrowed too: these servers answer four numbers, and the
    check that a vector fits the column is a test of its own.
    """
    monkeypatch.setattr("enggraph.embedder.EMBED_SERVER_URL", "")
    monkeypatch.setattr("enggraph.embedder.EMBED_LOCAL_URL", "")
    monkeypatch.setattr("enggraph.embedder.EMBED_DIM", 4)


def test_a_batch_comes_back_one_vector_per_chunk(server: HTTPServer) -> None:
    """Which is what keeps a vector from landing on the wrong chunk."""
    vectors = Embedder(stored_url=url_of(server)).embed(["one", "two", "three"])
    assert len(vectors) == 3
    assert vectors[0] == [0.5] * 4


def test_the_model_travels_with_the_request(server: HTTPServer) -> None:
    """Two models never share a vector space, so the row records which."""
    Embedder(stored_url=url_of(server), model="nomic").embed_one("query")
    assert server.seen[-1]["model"] == "nomic"


def test_a_dead_address_falls_through_to_the_next(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment pair is a chain: the far server, then the near one."""
    monkeypatch.setattr("enggraph.embedder.EMBED_SERVER_URL", DEAD)
    monkeypatch.setattr("enggraph.embedder.EMBED_LOCAL_URL", url_of(server))
    embedder = Embedder()
    assert embedder.urls == [DEAD, url_of(server)]
    assert embedder.embed_one("query") == [0.5] * 4
    assert embedder.chosen == url_of(server)


def test_a_stored_address_is_the_only_one_tried(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming a server is an instruction, not a preference.

    Falling through to the container because the named server refused is how
    the work quietly moved onto a CPU while a GPU sat idle - and nothing said
    so, because the fallback answered.
    """
    monkeypatch.setattr("enggraph.embedder.EMBED_LOCAL_URL", url_of(server))
    embedder = Embedder(stored_url=DEAD)
    assert embedder.urls == [DEAD]
    with pytest.raises(EmbedError):
        embedder.embed_one("query")
    assert server.seen == []


def test_nothing_answering_is_an_error_naming_the_remedy() -> None:
    """The caller releases its files rather than failing them, on this."""
    with pytest.raises(EmbedError) as refused:
        Embedder(stored_url=DEAD).embed_one("query")
    assert "no embedding server answered" in str(refused.value)
    assert "--embeddings" in str(refused.value)


def test_a_server_that_answers_an_error_is_no_answer(server: HTTPServer) -> None:
    """A 500 from one address is not a reason to stop trying the next."""
    server.status = 503  # type: ignore[attr-defined]
    with pytest.raises(EmbedError):
        Embedder(stored_url=url_of(server)).embed_one("query")


def test_availability_is_not_asked_again_within_the_window() -> None:
    """A queue with nowhere to go must not dial a dead address per file."""
    embedder = Embedder(stored_url=DEAD, probe_seconds=3600)
    assert embedder.available() is False
    assert embedder.available() is False


def test_an_empty_batch_asks_nothing(server: HTTPServer) -> None:
    """A file of nothing but whitespace has no chunks and costs no request."""
    assert Embedder(stored_url=url_of(server)).embed([]) == []
    assert server.seen == []


def test_a_batch_that_times_out_is_halved_rather_than_failed(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server is there and working; it was handed more than it could finish.

    Halving is what finds the size that fits without anyone having to know the
    hardware. Only a single chunk that still times out is worth reporting.
    """
    sizes: list[int] = []
    real = embedder_module.post

    def slow(url: str, body: dict, timeout: float, key: str = "") -> dict:
        count = len(body["input"])
        sizes.append(count)
        if count > 2:
            raise TimeoutError("timed out")
        return real(url, body, timeout, key)

    monkeypatch.setattr(embedder_module, "post", slow)
    vectors = Embedder(stored_url=url_of(server)).embed(["a", "b", "c", "d"])
    assert len(vectors) == 4
    assert sizes == [4, 2, 2]


def test_one_chunk_that_times_out_is_reported(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is nothing left to halve, and the reason names the timeout."""

    def slow(url: str, body: dict, timeout: float, key: str = "") -> dict:
        raise TimeoutError("timed out")

    monkeypatch.setattr(embedder_module, "post", slow)
    with pytest.raises(EmbedTimeout) as timed_out:
        Embedder(stored_url=url_of(server)).embed(["only one"])
    assert "did not finish 1 chunk(s)" in str(timed_out.value)


def test_a_short_answer_is_refused_rather_than_written() -> None:
    """A vector landing on the wrong chunk is an error no query reports."""
    with pytest.raises(EmbedError):
        read_vectors({"data": [{"embedding": [1.0]}]}, 2)


def test_the_other_llama_route_is_not_mistaken_for_this_one() -> None:
    """llama.cpp's own /embedding answers a different shape."""
    with pytest.raises(EmbedError):
        read_vectors({"embedding": [1.0, 2.0]}, 1)


def test_the_addresses_are_tried_in_order_and_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated URL is one address, not two attempts at it.

    The environment is cleared here rather than trusted: these two variables
    are set on a machine that runs the stack, and the list is what they and
    the stored URL make together.
    """
    monkeypatch.setattr("enggraph.embedder.EMBED_SERVER_URL", "http://a:1")
    monkeypatch.setattr("enggraph.embedder.EMBED_LOCAL_URL", "http://b:2")
    assert candidates("http://a:1/") == ["http://a:1"]
    assert candidates("") == ["http://a:1", "http://b:2"]


def test_a_stored_key_travels_as_a_bearer_token(server: HTTPServer) -> None:
    """Which is what `llama-server --api-key` checks."""
    Embedder(stored_url=url_of(server), stored_key="secret").embed_one("query")
    assert server.headers_seen[-1]["Authorization"] == "Bearer secret"


def test_no_key_sends_no_header(server: HTTPServer) -> None:
    """A server without --api-key must not be handed an empty credential."""
    Embedder(stored_url=url_of(server)).embed_one("query")
    assert "Authorization" not in server.headers_seen[-1]


def test_a_refused_token_says_so_rather_than_saying_nothing_answered(
    server: HTTPServer,
) -> None:
    """The remedy differs: one is a key to fix, the other a server to start."""
    server.status = 401  # type: ignore[attr-defined]
    with pytest.raises(EmbedError) as refused:
        Embedder(stored_url=url_of(server), stored_key="wrong").embed_one("query")
    assert "refused the token (401)" in str(refused.value)


def test_a_server_that_refuses_the_route_says_which_one_and_why(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat server answers 501 here, and that is an answer, not silence.

    This is the case the suite did not have. Falling through to the next
    address was tested as a feature; that it happened without a word was not,
    and a queue quietly moved onto a CPU while a GPU sat idle.
    """
    refusing = url_of(server)
    server.status = 501  # type: ignore[attr-defined]
    embedder = Embedder(stored_url=refusing)
    with pytest.raises(EmbedError):
        embedder.embed_one("query")
    assert refusing in embedder.refusals
    assert "501" in embedder.refusals[refusing]
    assert "does not support embeddings" in embedder.refusals[refusing]


def test_the_reason_travels_instead_of_start_a_server(server: HTTPServer) -> None:
    """The remedy for a refusal is not the remedy for an address nobody runs."""
    server.status = 501  # type: ignore[attr-defined]
    with pytest.raises(EmbedError) as refused:
        Embedder(stored_url=url_of(server)).embed_one("query")
    assert "refused the embeddings route" in str(refused.value)
    assert "Start the bundled one" not in str(refused.value)


def test_falling_back_records_what_the_skipped_address_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the environment chain falls back, and it says what it skipped."""
    refusing = HTTPServer(("127.0.0.1", 0), Handler)
    refusing.seen = []  # type: ignore[attr-defined]
    refusing.headers_seen = []  # type: ignore[attr-defined]
    refusing.status = 501  # type: ignore[attr-defined]
    refusing.message = "This server does not support embeddings"  # type: ignore[attr-defined]
    refusing.width = 4  # type: ignore[attr-defined]
    working = HTTPServer(("127.0.0.1", 0), Handler)
    working.seen = []  # type: ignore[attr-defined]
    working.headers_seen = []  # type: ignore[attr-defined]
    working.status = 200  # type: ignore[attr-defined]
    working.message = ""  # type: ignore[attr-defined]
    working.width = 4  # type: ignore[attr-defined]
    for one in (refusing, working):
        threading.Thread(target=one.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr("enggraph.embedder.EMBED_SERVER_URL", url_of(refusing))
        monkeypatch.setattr("enggraph.embedder.EMBED_LOCAL_URL", url_of(working))
        embedder = Embedder()
        assert embedder.embed_one("query") == [0.5] * 4
        assert embedder.chosen == url_of(working)
        assert url_of(refusing) in embedder.refusals
    finally:
        for one in (refusing, working):
            one.shutdown()
            one.server_close()


def test_a_vector_of_the_wrong_width_is_refused_with_the_reason(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server running another model cannot be written to this column at all.

    Caught before the insert: the database would refuse it one file at a time
    and the reason would read as a type error rather than as the wrong model.
    """
    monkeypatch.setattr("enggraph.embedder.EMBED_DIM", 768)
    server.width = 1536  # type: ignore[attr-defined]
    with pytest.raises(EmbedError) as refused:
        Embedder(stored_url=url_of(server)).embed_one("query")
    assert "1536-dimensional" in str(refused.value)
    assert "768" in str(refused.value)
