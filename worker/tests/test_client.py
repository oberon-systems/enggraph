"""Where the worker's token and the model server's key may travel."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator

import pytest
from enggraph_worker.client import Client, PlainHttp
from enggraph_worker.server import ServerRunner


class Recorder(http.server.BaseHTTPRequestHandler):
    """Answer JSON, or send whoever asks on to the other server."""

    seen: list[tuple[int, str | None]] = []
    target = ""

    def do_GET(self) -> None:  # noqa: N802 - the name is http.server's
        """Record the Authorization header this request came with."""
        self.seen.append((self.server.server_port, self.headers.get("Authorization")))
        if self.path.startswith("/moved"):
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()
            return
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def servers() -> Iterator[tuple[int, int]]:
    """Run two servers on loopback: one redirects every /moved to the other."""
    Recorder.seen = []
    running = [
        http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder) for _ in range(2)
    ]
    Recorder.target = f"http://127.0.0.1:{running[1].server_port}/health"
    for one in running:
        threading.Thread(target=one.serve_forever, daemon=True).start()
    yield running[0].server_port, running[1].server_port
    for one in running:
        one.shutdown()
        one.server_close()


def test_a_token_does_not_leave_the_machine_over_http() -> None:
    """Refused when the client is made, before anything is sent."""
    with pytest.raises(PlainHttp, match="--allow-plain-http"):
        Client("http://192.0.2.10/worker", "secret")
    with pytest.raises(PlainHttp):
        Client("ftp://localhost/worker", "secret")
    for url in (
        "https://192.0.2.10/worker",
        "http://127.0.0.1:3000/worker",
        "http://localhost:3000/worker",
        "http://[::1]:3000/worker",
    ):
        Client(url, "secret")
    Client("http://192.0.2.10/worker", "secret", allow_plain_http=True)


def test_a_model_server_key_is_held_to_the_same_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that takes no key carries no secret, and is not refused."""
    monkeypatch.setattr(ServerRunner, "call", lambda self, path: {})
    with pytest.raises(PlainHttp):
        ServerRunner("http://192.0.2.10:8080", api_key="secret")
    ServerRunner("http://192.0.2.10:8080")
    ServerRunner("http://192.0.2.10:8080", api_key="secret", allow_plain_http=True)


def test_a_redirect_to_another_origin_carries_no_secret(
    servers: tuple[int, int],
) -> None:
    """The token belongs to the address it was configured for."""
    first, second = servers
    api = Client(f"http://127.0.0.1:{first}", "secret")
    assert api.call("/moved") == {"status": "ok"}
    assert Recorder.seen == [(first, "Bearer secret"), (second, None)]
    Recorder.seen.clear()
    runner = ServerRunner(f"http://127.0.0.1:{first}", api_key="key")
    Recorder.seen.clear()
    assert runner.call("/moved") == {"status": "ok"}
    assert Recorder.seen == [(first, "Bearer key"), (second, None)]
