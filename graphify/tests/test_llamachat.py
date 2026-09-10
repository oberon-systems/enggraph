"""Asking a llama.cpp server for a sentence, over the addresses configured.

A real HTTP server on a loopback port, as in test_embedder: what is worth
pinning is the fallback from an address that refuses a connection, and a mock
cannot refuse one.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from enggraph.llamachat import Chat, ChatError, candidates

DEAD = "http://127.0.0.1:1"


class Handler(BaseHTTPRequestHandler):
    """Answer /props and /v1/chat/completions the way llama-server does."""

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        """Report what this server is running, or refuse the token."""
        served = self.server  # type: ignore[attr-defined]
        served.headers_seen.append(dict(self.headers))
        if served.unauthorized:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self._send(
            {
                "model_path": "/models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",
                "default_generation_settings": {"n_ctx": 8192},
                "total_slots": 1,
            }
        )

    def do_POST(self) -> None:  # noqa: N802 - the base class names it
        """Answer with one sentence, or with the failure the test asked for."""
        length = int(self.headers.get("Content-Length", "0"))
        served = self.server  # type: ignore[attr-defined]
        served.seen.append(json.loads(self.rfile.read(length) or b"{}"))
        served.headers_seen.append(dict(self.headers))
        if served.empty:
            self._send({"choices": []})
            return
        self._send({"choices": [{"message": {"content": "Reads the queue."}}]})

    def log_message(self, *_: object) -> None:
        """Keep the suite's output to the suite's own."""


@pytest.fixture(autouse=True)
def _no_environment_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's own SUMMARIZE_SERVER_URL out of these tests."""
    monkeypatch.setattr("enggraph.llamachat.SUMMARIZE_SERVER_URL", "")


@pytest.fixture
def server() -> Iterator[HTTPServer]:
    """One chat server on a port the kernel picks."""
    running = HTTPServer(("127.0.0.1", 0), Handler)
    running.seen = []  # type: ignore[attr-defined]
    running.headers_seen = []  # type: ignore[attr-defined]
    running.empty = False  # type: ignore[attr-defined]
    running.unauthorized = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    yield running
    running.shutdown()
    running.server_close()


def url_of(server: HTTPServer) -> str:
    """Return the base address of a running server."""
    return f"http://127.0.0.1:{server.server_port}"


def test_props_names_the_model_and_the_context(server: HTTPServer) -> None:
    """Which is what the settings page shows beside a URL before storing it."""
    chat = Chat(stored_url=url_of(server))
    chat.props()
    assert chat.model == "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
    assert chat.n_ctx == 8192
    assert chat.chosen == url_of(server)


def test_the_prompt_and_the_system_message_both_travel(server: HTTPServer) -> None:
    """The wording is the API's, so a change to it is not a worker redeploy."""
    answer = Chat(stored_url=url_of(server)).ask("be brief", "File: a.py\n\ncode", 64)
    assert answer == "Reads the queue."
    sent = server.seen[-1]
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["messages"][1]["content"].startswith("File: a.py")
    assert sent["max_tokens"] == 64


def test_a_stored_address_is_the_only_one_tried(
    server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming a server is an instruction, not a preference.

    The environment address is the fallback for a stack configured by file,
    and it must not quietly take work away from the address somebody typed.
    """
    monkeypatch.setattr("enggraph.llamachat.SUMMARIZE_SERVER_URL", url_of(server))
    chat = Chat(stored_url=DEAD)
    assert chat.urls == [DEAD]
    with pytest.raises(ChatError):
        chat.ask("s", "p", 8)
    assert server.seen == []


def test_nothing_answering_names_the_remedy() -> None:
    """The loop hands its batch back on this, rather than failing the files."""
    with pytest.raises(ChatError) as refused:
        Chat(stored_url=DEAD).ask("s", "p", 8)
    assert "no llama.cpp server answered" in str(refused.value)


def test_an_answer_without_a_choice_is_an_error(server: HTTPServer) -> None:
    """An empty completion is not a summary, and must not be shaped into one."""
    server.empty = True  # type: ignore[attr-defined]
    with pytest.raises(ChatError):
        Chat(stored_url=url_of(server)).ask("s", "p", 8)


def test_availability_is_not_asked_again_within_the_window() -> None:
    """A queue with nowhere to push must not dial a dead address per tick."""
    chat = Chat(stored_url=DEAD, probe_seconds=3600)
    assert chat.available() is False
    assert chat.available() is False


def test_no_address_configured_is_no_addresses_at_all() -> None:
    """Which is what keeps the loop from pushing at nobody."""
    assert candidates("") == []


def test_a_stored_key_travels_as_a_bearer_token(server: HTTPServer) -> None:
    """Which is what `llama-server --api-key` checks."""
    Chat(stored_url=url_of(server), stored_key="secret").props()
    assert server.headers_seen[-1]["Authorization"] == "Bearer secret"


def test_a_refused_token_is_reported_as_a_key_rather_than_a_silence(
    server: HTTPServer,
) -> None:
    """A 401 has a remedy of its own, and it is not "start a server"."""
    server.unauthorized = True  # type: ignore[attr-defined]
    with pytest.raises(ChatError) as refused:
        Chat(stored_url=url_of(server), stored_key="wrong").props()
    assert "refused the token (401)" in str(refused.value)


def test_a_failed_ask_opens_the_quiet_window() -> None:
    """A drain skips a dead server instead of dialling it for every file."""
    chat = Chat(stored_url=DEAD, probe_seconds=3600)
    with pytest.raises(ChatError):
        chat.ask("s", "p", 8)
    assert chat.available() is False


def test_a_dead_server_is_reported_once_per_outage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log says it went away, not that it is still away every minute."""
    chat = Chat(stored_url=DEAD, probe_seconds=0)
    with caplog.at_level("INFO", logger="enggraph.llamachat"):
        assert chat.available() is False
        assert chat.available() is False
        with pytest.raises(ChatError):
            chat.ask("s", "p", 8)
    said = [r for r in caplog.records if "no llama.cpp server answered" in r.message]
    assert len(said) == 1
