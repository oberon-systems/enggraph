"""The hub: framing, requests over a socket, and a tree read through it."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enggraph.api import agenthub, workerapi
from enggraph.api.agenthub import Hub
from enggraph.core import trees

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "packages/core/tests/data/agent_protocol_examples.json"
NGINX = ROOT / "nginx/default.conf"
SOURCE = trees.Source("alpha", "agent-1", "alpha-export", "/srv/alpha", True)


class FakeSocket:
    """The agent's end: every request sent over it goes to a handler of the test's."""

    def __init__(self, hub: Hub, handler: Handler) -> None:
        """Take what answers the requests."""
        self.hub = hub
        self.handler = handler
        self.connection: agenthub.Connection | None = None
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, data: str) -> None:
        """Hand a request to the handler, which answers through the connection."""
        request = json.loads(data)
        self.sent.append(request)
        assert self.connection is not None
        self.handler(self.connection, request)


Handler = Callable[[agenthub.Connection, dict[str, Any]], None]


def data_frame(number: int, last: bool, payload: bytes) -> bytes:
    """Spell a binary frame the way the agent does."""
    return number.to_bytes(4, "big") + bytes([1 if last else 0]) + payload


class Stand:
    """An event loop on a thread, as uvicorn gives the socket, and a hub on it."""

    def __init__(self) -> None:
        """Start the loop."""
        self.hub = Hub()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def attach(self, handler: Handler, agent_id: str = "agent-1") -> FakeSocket:
        """Connect an agent."""
        socket = FakeSocket(self.hub, handler)

        async def attach() -> None:
            socket.connection = self.hub.attach(agent_id, socket)

        asyncio.run_coroutine_threadsafe(attach(), self.loop).result(timeout=5)
        return socket

    def detach(self, socket: FakeSocket) -> None:
        """Take an agent's socket away."""
        assert socket.connection is not None
        connection = socket.connection

        async def detach() -> None:
            self.hub.detach(connection)

        asyncio.run_coroutine_threadsafe(detach(), self.loop).result(timeout=5)

    def stop(self) -> None:
        """End the loop."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


@pytest.fixture
def stand(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stand]:
    """Return a running hub, and make it what trees are read through."""
    running = Stand()
    monkeypatch.setattr(trees, "_transport", running.hub)
    monkeypatch.setattr(agenthub, "RECONNECT_SECONDS", 1)
    monkeypatch.setattr(agenthub, "BUSY_PAUSE_SECONDS", 0.001)
    yield running
    running.stop()


def ok(result: dict[str, Any]) -> Handler:
    """Answer every request with one result."""

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        connection.answer({"id": request["id"], "ok": True, "result": result})

    return handler


def test_a_data_frame_reads_as_the_shared_examples_say() -> None:
    """The agent's tests hold the same examples."""
    for case in json.loads(EXAMPLES.read_text())["data"]:
        frame = bytes.fromhex(case["frame_hex"])
        assert agenthub.parse_data(frame) == (
            case["id"],
            case["last"],
            bytes.fromhex(case["payload_hex"]),
        )


def test_a_frame_too_short_is_no_frame() -> None:
    """Nothing to read an id from."""
    assert agenthub.parse_data(b"\x00\x00") is None


def test_a_request_is_spelled_as_the_shared_examples_say(stand: Stand) -> None:
    """The fields the agent reads, and nothing else."""
    socket = stand.attach(ok({"stats": []}))
    stand.hub.call(SOURCE, "stat", {"export": "alpha-export", "paths": []})
    assert socket.sent == [
        {"id": 1, "op": "stat", "args": {"export": "alpha-export", "paths": []}}
    ]
    for case in json.loads(EXAMPLES.read_text())["requests"]:
        assert set(json.loads(case["text"])) == {"id", "op", "args"}


def test_batches_are_put_together(stand: Stand) -> None:
    """What is a list goes on until the answer without `more`."""

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        number = request["id"]
        result = {"entries": ["a"], "dirs": ["b"]}
        connection.answer({"id": number, "ok": True, "result": result, "more": True})
        result = {"entries": ["c"], "dirs": []}
        connection.answer({"id": number, "ok": True, "result": result})

    stand.attach(handler)
    assert stand.hub.call(SOURCE, "walk", {}) == {"entries": ["a", "c"], "dirs": ["b"]}


def test_data_frames_are_put_together(stand: Stand) -> None:
    """Until the one with the last flag."""

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        connection.data(data_frame(request["id"], False, b"one "))
        connection.data(data_frame(request["id"], True, b"two"))

    stand.attach(handler)
    assert stand.hub.data(SOURCE, "read", {}) == b"one two"


def test_answers_come_back_to_who_asked_in_any_order(stand: Stand) -> None:
    """Requests are multiplexed over one socket."""
    held: list[tuple[agenthub.Connection, int, str]] = []

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        held.append((connection, request["id"], request["args"]["name"]))
        if len(held) == 2:
            for owner, number, name in reversed(held):
                owner.answer({"id": number, "ok": True, "result": {"name": name}})

    stand.attach(handler)
    answers: dict[str, Any] = {}

    def ask(name: str) -> None:
        answers[name] = stand.hub.call(SOURCE, "stat", {"name": name})

    threads = [threading.Thread(target=ask, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert answers == {"a": {"name": "a"}, "b": {"name": "b"}}


def test_a_refusal_carries_its_code(stand: Stand) -> None:
    """What the agent said, for whoever asked to tell apart."""

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        error = {"code": "denied", "message": "denied"}
        connection.answer({"id": request["id"], "ok": False, "error": error})

    stand.attach(handler)
    with pytest.raises(trees.TreeRefused) as refused:
        stand.hub.data(SOURCE, "read", {})
    assert refused.value.code == "denied"


def test_busy_is_asked_again(stand: Stand) -> None:
    """A burst is no refusal: it is waited out, a bounded number of times."""
    tries: list[int] = []

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        tries.append(request["id"])
        number = request["id"]
        if len(tries) < 3:
            error = {"code": "busy", "message": "busy"}
            connection.answer({"id": number, "ok": False, "error": error})
        else:
            connection.answer({"id": number, "ok": True, "result": {"done": 1}})

    stand.attach(handler)
    assert stand.hub.call(SOURCE, "stat", {}) == {"done": 1}
    assert len(tries) == 3


def test_busy_for_good_is_a_refusal(stand: Stand) -> None:
    """It stops being asked."""

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        error = {"code": "busy", "message": "busy"}
        connection.answer({"id": request["id"], "ok": False, "error": error})

    socket = stand.attach(handler)
    with pytest.raises(trees.TreeRefused):
        stand.hub.call(SOURCE, "stat", {})
    assert len(socket.sent) == agenthub.BUSY_ATTEMPTS


def test_a_lost_socket_is_asked_again_once_the_agent_dials_back(stand: Stand) -> None:
    """What every credential rotation does to a request in flight."""
    sockets: list[FakeSocket] = []
    redial = threading.Event()

    def lost(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        connection.close()
        stand.hub.detach(connection)
        redial.set()

    sockets.append(stand.attach(lost))

    def dial() -> None:
        redial.wait(timeout=5)
        sockets.append(stand.attach(ok({"back": 1})))

    dialling = threading.Thread(target=dial)
    dialling.start()
    assert stand.hub.call(SOURCE, "stat", {}) == {"back": 1}
    dialling.join(timeout=5)


def test_an_agent_that_stays_away_is_unavailable(stand: Stand) -> None:
    """After a moment's patience."""
    with pytest.raises(trees.TreeUnavailable):
        stand.hub.call(SOURCE, "stat", {})
    assert not stand.hub.connected(SOURCE)


def test_a_socket_replaced_does_not_take_the_new_one_with_it(stand: Stand) -> None:
    """The old one ends after the agent has already dialled again."""
    old = stand.attach(ok({"which": "old"}))
    stand.attach(ok({"which": "new"}))
    stand.detach(old)
    assert stand.hub.connected(SOURCE)
    assert stand.hub.call(SOURCE, "stat", {}) == {"which": "new"}


def test_a_late_timeout_says_the_tree_cannot_answer(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that hears and says nothing must not hold a run forever."""
    monkeypatch.setattr(agenthub, "DEFAULT_SECONDS", 0.01)
    stand.attach(lambda connection, request: None)
    with pytest.raises(trees.TreeUnavailable):
        stand.hub.call(SOURCE, "stat", {})


def test_the_event_loop_may_not_wait_on_itself(stand: Stand) -> None:
    """It is the thread that would have to deliver the answer."""
    stand.attach(ok({}))

    async def ask() -> None:
        stand.hub.call(SOURCE, "stat", {})

    with pytest.raises(RuntimeError):
        asyncio.run_coroutine_threadsafe(ask(), stand.loop).result(timeout=5)


def test_events_reach_who_listens(stand: Stand) -> None:
    """A change by its paths, a resync by none."""
    socket = stand.attach(ok({}))
    heard: list[tuple[str, str, list[str] | None]] = []
    stand.hub.subscribe(lambda *event: heard.append(event))
    assert socket.connection is not None
    socket.connection.event(
        {"type": "changed", "export": "alpha-export", "paths": ["a.py"]}
    )
    socket.connection.event({"type": "resync", "export": "alpha-export"})
    assert heard == [
        ("agent-1", "alpha-export", ["a.py"]),
        ("agent-1", "alpha-export", None),
    ]


def test_what_nobody_heard_is_owed_as_a_full_pass(stand: Stand) -> None:
    """An agent reconnecting before the watch stands must not be lost."""
    socket = stand.attach(ok({}))
    assert socket.connection is not None
    socket.connection.event({"type": "changed", "export": "alpha-export", "paths": []})
    heard: list[tuple[str, str, list[str] | None]] = []
    stop = stand.hub.subscribe(lambda *event: heard.append(event))
    assert heard == [("agent-1", "alpha-export", None)]
    stop()
    assert stand.hub.listeners == []


def test_a_tree_reads_through_the_hub_end_to_end(stand: Stand) -> None:
    """Every `Tree` operation over a socket, with a fake agent on its far end."""

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        number, op = request["id"], request["op"]
        args = request["args"]
        assert args["export"] == "alpha-export"
        if op == "walk":
            entries = [{"path": "a.py", "size": 1, "mtime": 1, "md5": None}]
            result = (
                {"entries": entries, "dirs": ["b"], "skipped": []}
                if args["top"] == ""
                else {"entries": [], "dirs": [], "skipped": []}
            )
            connection.answer({"id": number, "ok": True, "result": result})
        elif op == "stat":
            stat = {"is_file": True, "is_dir": False, "size": 7, "contained": True}
            result = {"stats": [{"path": args["paths"][0], **stat}]}
            connection.answer({"id": number, "ok": True, "result": result})
        elif op == "grep":
            match = {"path": "a.py", "line": 1, "text": "hit"}
            result = {"matches": [match]}
            connection.answer({"id": number, "ok": True, "result": result})
        elif op == "head":
            connection.data(data_frame(number, True, b"#!/"))
        else:
            connection.data(data_frame(number, True, b"one\r\n"))

    stand.attach(handler)
    tree = trees.RemoteTree(SOURCE)
    assert tree.available()
    assert list(tree.walk()) == ["a.py"]
    assert tree.is_file("a.py") and not tree.is_dir("a.py")
    assert tree.contains("a.py")
    assert tree.size("a.py") == 7
    assert tree.head("a.py", 3) == b"#!/"
    assert tree.read("a.py") == ("one\n", "")
    assert tree.grep("hit", [], None, 5, False) == [("a.py", 1, "hit")]


def _client(monkeypatch: pytest.MonkeyPatch, stand: Stand) -> TestClient:
    monkeypatch.setattr(workerapi, "WORKER_API_TOKEN", "alpha-token-for-tests")
    monkeypatch.setattr(agenthub, "HUB", stand.hub)
    monkeypatch.setattr(trees, "source", lambda project, fresh=False: SOURCE)
    app = FastAPI()
    app.include_router(workerapi.api)
    return TestClient(app, headers={"Authorization": "Bearer alpha-token-for-tests"})


def test_the_internal_routes_need_the_token(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This reads the text of your files."""
    client = _client(monkeypatch, stand)
    client.headers.pop("Authorization")
    assert client.post("/internal/trees/alpha/available", json={}).status_code == 401


def test_the_internal_routes_answer_for_the_export_of_the_binding(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller names a project; which export it reads is not its to say."""
    socket = stand.attach(ok({"stats": []}))
    client = _client(monkeypatch, stand)
    asked = {"export": "beta-export", "paths": []}
    answer = client.post("/internal/trees/alpha/stat", json=asked)
    assert answer.status_code == 200
    assert answer.json() == {"stats": []}
    assert socket.sent[0]["args"]["export"] == "alpha-export"


def test_the_internal_routes_hand_bytes_and_a_refusal_apart(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied file and a tree that is gone must not read alike."""

    def handler(connection: agenthub.Connection, request: dict[str, Any]) -> None:
        if request["args"]["path"] == "a.py":
            connection.data(data_frame(request["id"], True, b"\xffbytes"))
        else:
            error = {"code": "denied", "message": "denied"}
            connection.answer({"id": request["id"], "ok": False, "error": error})

    stand.attach(handler)
    client = _client(monkeypatch, stand)
    answer = client.post("/internal/trees/alpha/read", json={"path": "a.py"})
    assert answer.content == b"\xffbytes"
    refused = client.post("/internal/trees/alpha/read", json={"path": ".env"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "denied"


def test_the_internal_routes_say_when_the_agent_is_gone(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 503, which the other containers read as an unavailable tree."""
    client = _client(monkeypatch, stand)
    available = client.post("/internal/trees/alpha/available", json={})
    assert available.json() == {"available": False}
    gone = client.post("/internal/trees/alpha/stat", json={"paths": []})
    assert gone.status_code == 503


def test_the_internal_routes_offer_nothing_else(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no write to forward."""
    client = _client(monkeypatch, stand)
    assert client.post("/internal/trees/alpha/write", json={}).status_code == 404


def test_the_gateway_does_not_route_the_internal_routes() -> None:
    """`/worker/` injects the token, so it would hand the routes to anyone it passes."""
    conf = NGINX.read_text()
    block = conf[conf.index("location ^~ /worker/internal/") :]
    assert "return 404;" in block[: block.index("}")]
