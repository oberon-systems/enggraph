"""One request, four timeouts: each phase of it is bounded on its own."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from enggraph.config import Timeouts
from enggraph.dial import Unreachable, request_json

FAST = Timeouts(connect=1, write=1, read=1, total=2)


class Handler(BaseHTTPRequestHandler):
    """Answer the way the test asked: at once, late, a trickle, or a 401."""

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        """Behave as `server.mode` says."""
        self._answer()

    def do_POST(self) -> None:  # noqa: N802 - the base class names it
        """Behave as `server.mode` says, echoing the request body."""
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.body = body  # type: ignore[attr-defined]
        self._answer()

    def _answer(self) -> None:
        served = self.server
        served.headers_seen = dict(self.headers)  # type: ignore[attr-defined]
        mode = served.mode  # type: ignore[attr-defined]
        if mode == "late":
            time.sleep(1.0)
        payload = json.dumps({"ok": True}).encode("utf-8")
        status = 401 if mode == "refuse" else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        padding = 20 if mode == "trickle" else 0
        self.send_header("Content-Length", str(len(payload) + padding))
        self.end_headers()
        if mode == "trickle":
            for _ in range(padding):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.1)
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:
        """Keep the suite's output to the suite's own."""


@pytest.fixture
def server() -> Iterator[ThreadingHTTPServer]:
    """One server on a port the kernel picks."""
    running = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    running.daemon_threads = True
    running.mode = "ok"  # type: ignore[attr-defined]
    threading.Thread(target=running.serve_forever, daemon=True).start()
    yield running
    running.shutdown()
    running.server_close()


def url_of(server: ThreadingHTTPServer) -> str:
    """Return the base address of a running server."""
    return f"http://127.0.0.1:{server.server_port}"


def test_an_answer_comes_back_as_json(server: ThreadingHTTPServer) -> None:
    """The ordinary case, a POST with a body and a bearer token."""
    answer = request_json(f"{url_of(server)}/x", {"a": 1}, FAST, "secret")
    assert answer == {"ok": True}
    assert server.body == {"a": 1}  # type: ignore[attr-defined]
    seen = server.headers_seen  # type: ignore[attr-defined]
    assert seen["Authorization"] == "Bearer secret"


def test_no_body_is_a_get_and_no_key_is_no_header(server: ThreadingHTTPServer) -> None:
    """A probe of /props reads, and an unkeyed server is handed no credential."""
    assert request_json(f"{url_of(server)}/props", None, FAST) == {"ok": True}
    assert "Authorization" not in server.headers_seen  # type: ignore[attr-defined]


def test_a_refused_connection_is_unreachable_at_once() -> None:
    """Nothing listens on port 1, and saying so takes no timeout at all."""
    started = time.monotonic()
    with pytest.raises(Unreachable):
        request_json("http://127.0.0.1:1/x", {}, FAST)
    assert time.monotonic() - started < 1


def test_a_connect_that_times_out_is_unreachable_not_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable host must never read as a server busy with the work."""

    def hang(self: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr("http.client.HTTPConnection.connect", hang)
    with pytest.raises(Unreachable) as refused:
        request_json("http://10.255.255.1:8080/x", {}, FAST)
    assert not isinstance(refused.value, TimeoutError)


def test_a_server_that_does_not_answer_in_time_is_a_read_timeout(
    server: ThreadingHTTPServer,
) -> None:
    """The connection was made; the answer is what did not come."""
    server.mode = "late"  # type: ignore[attr-defined]
    with pytest.raises(TimeoutError, match="read timeout"):
        request_json(url_of(server), {}, Timeouts(1, 1, read=0.2, total=5))


def test_a_trickle_is_cut_off_by_the_total(server: ThreadingHTTPServer) -> None:
    """Each byte arrives inside the read timeout; the whole does not."""
    server.mode = "trickle"  # type: ignore[attr-defined]
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="total timeout"):
        request_json(url_of(server), {}, Timeouts(1, 1, read=1, total=0.5))
    assert time.monotonic() - started < 1.5


def test_an_error_status_is_an_http_error_with_its_body(
    server: ThreadingHTTPServer,
) -> None:
    """The callers tell a refused token from silence by the code."""
    server.mode = "refuse"  # type: ignore[attr-defined]
    with pytest.raises(urllib.error.HTTPError) as refused:
        request_json(url_of(server), {}, FAST)
    assert refused.value.code == 401
    assert json.loads(refused.value.read()) == {"ok": True}


def test_a_url_that_is_not_http_is_unreachable() -> None:
    """Refused before any socket is opened."""
    with pytest.raises(Unreachable):
        request_json("ftp://example.org/x", {}, FAST)


def test_the_socket_module_timeout_is_the_builtin_one() -> None:
    """The phases catch TimeoutError; socket.timeout must be that same class."""
    assert socket.timeout is TimeoutError
