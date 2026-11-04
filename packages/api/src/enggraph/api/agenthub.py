"""The sockets host agents hold open, and the requests that travel over them.

Everything here runs on the event loop except the calls of `Hub` a worker thread
makes, which hand a coroutine over and wait. All tree operations are reads, so
one that loses its socket halfway, as every credential rotation does, is sent
again once the agent has dialled back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from starlette.websockets import WebSocketDisconnect

from enggraph.core.trees import Listener, Source, TreeRefused, TreeUnavailable

log = logging.getLogger(__name__)

HEADER_BYTES = 5
LAST = 1
MAX_REQUEST_ID = 2**32 - 1
# Below the agent's own cap, so `busy` means another caller, not a burst of ours.
MAX_IN_FLIGHT = 32
MAX_DATA_BYTES = 512 * 1024 * 1024
BUSY = "busy"
BUSY_ATTEMPTS = 8
BUSY_PAUSE_SECONDS = 0.25
RECONNECT_SECONDS = 15
RECONNECT_POLL_SECONDS = 0.2
DEFAULT_SECONDS = 60
OP_SECONDS = {"walk": 300, "grep": 70, "fetch": 300}


class Socket(Protocol):
    """What the hub needs of a WebSocket."""

    async def send_text(self, data: str) -> None:
        """Send one text frame."""


class ConnectionLost(TreeUnavailable):
    """The socket ended with this request unanswered."""


def parse_data(frame: bytes) -> tuple[int, bool, bytes] | None:
    """Read a binary frame as (request id, last, payload), or None if too short."""
    if len(frame) < HEADER_BYTES:
        return None
    number = int.from_bytes(frame[:4], "big")
    return number, bool(frame[4] & LAST), frame[HEADER_BYTES:]


def on_event_loop() -> bool:
    """Say whether this thread runs a loop, which waiting here would block forever."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def merged(into: dict[str, Any], batch: dict[str, Any]) -> None:
    """Add one batch of an answer: what is a list goes on, the rest is replaced."""
    for key, value in batch.items():
        if isinstance(value, list) and isinstance(into.get(key), list):
            into[key].extend(value)
        else:
            into[key] = value


@dataclass
class Pending:
    """One request waiting for its answer."""

    done: asyncio.Future[Any]
    result: dict[str, Any] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0


Answer = dict[str, Any] | bytes


class Connection:
    """One agent's socket: its requests in flight, and a lock on what is sent."""

    def __init__(self, hub: Hub, agent_id: str, socket: Socket) -> None:
        """Take the socket of an agent that has been greeted."""
        self.hub = hub
        self.agent_id = agent_id
        self.socket = socket
        self.loop = asyncio.get_running_loop()
        self.pending: dict[int, Pending] = {}
        self.next_id = 0
        self.closed = False
        self.send_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(MAX_IN_FLIGHT)

    async def send(self, text: str) -> None:
        """Send one text frame; a socket takes them one at a time."""
        async with self.send_lock:
            await self.socket.send_text(text)

    def _number(self) -> int:
        self.next_id = 0 if self.next_id >= MAX_REQUEST_ID else self.next_id + 1
        return self.next_id

    async def request(self, op: str, args: dict[str, Any], seconds: float) -> Answer:
        """Send a request and wait for its whole answer."""
        async with self.slots:
            if self.closed:
                raise ConnectionLost(f"the socket of agent {self.agent_id} ended")
            number = self._number()
            pending = Pending(self.loop.create_future())
            self.pending[number] = pending
            try:
                frame = json.dumps({"id": number, "op": op, "args": args})
                await self.send(frame)
                async with asyncio.timeout(seconds):
                    return await pending.done
            except TimeoutError as late:
                raise TreeUnavailable(f"{op} took over {seconds} s") from late
            except (OSError, RuntimeError, WebSocketDisconnect) as error:
                # A socket closing under a send says so in any of these.
                raise ConnectionLost(str(error)) from error
            finally:
                self.pending.pop(number, None)

    def answer(self, frame: dict[str, object]) -> None:
        """Take a text answer: a batch, the last of them, or a refusal."""
        number = frame.get("id")
        pending = self.pending.get(number) if isinstance(number, int) else None
        if pending is None or pending.done.done():
            return
        if frame.get("ok") is not True:
            error = frame.get("error")
            error = error if isinstance(error, dict) else {}
            refused = TreeRefused(
                str(error.get("code", "")), str(error.get("message", ""))
            )
            pending.done.set_exception(refused)
            return
        batch = frame.get("result")
        merged(pending.result, batch if isinstance(batch, dict) else {})
        if not frame.get("more"):
            pending.done.set_result(pending.result)

    def data(self, frame: bytes) -> None:
        """Take a binary frame of an answer in bytes."""
        parsed = parse_data(frame)
        if parsed is None:
            return
        number, last, payload = parsed
        pending = self.pending.get(number)
        if pending is None or pending.done.done():
            return
        pending.size += len(payload)
        if pending.size > MAX_DATA_BYTES:
            pending.done.set_exception(
                TreeRefused("over the size limit", "the answer is too large")
            )
            return
        pending.chunks.append(payload)
        if last:
            pending.done.set_result(b"".join(pending.chunks))

    def event(self, frame: dict[str, object]) -> None:
        """Pass a change the agent heard on to whoever listens."""
        export = frame.get("export")
        if not isinstance(export, str):
            return
        paths = frame.get("paths")
        if frame.get("type") == "resync" or not isinstance(paths, list):
            self.hub.event(self.agent_id, export, None)
            return
        kept = [path for path in paths if isinstance(path, str)]
        self.hub.event(self.agent_id, export, kept)

    def close(self) -> None:
        """Fail what is waiting on this socket, which will never be answered."""
        self.closed = True
        for pending in self.pending.values():
            if not pending.done.done():
                pending.done.set_exception(
                    ConnectionLost(f"the socket of agent {self.agent_id} ended")
                )


class Hub:
    """Agent id to live connection, and the events of all of them."""

    def __init__(self) -> None:
        """Start with nobody connected and nobody listening."""
        self.lock = threading.Lock()
        self.connections: dict[str, Connection] = {}
        self.listeners: list[Listener] = []
        # What was heard while nobody listened, owed as a full pass.
        self.owed: set[tuple[str, str]] = set()

    def attach(self, agent_id: str, socket: Socket) -> Connection:
        """Hold a new socket; an agent that dialled again replaces its old one."""
        connection = Connection(self, agent_id, socket)
        with self.lock:
            replaced = self.connections.get(agent_id)
            self.connections[agent_id] = connection
        if replaced is not None:
            replaced.close()
        return connection

    def detach(self, connection: Connection) -> None:
        """Forget a socket that ended, unless it has already been replaced."""
        connection.close()
        with self.lock:
            if self.connections.get(connection.agent_id) is connection:
                del self.connections[connection.agent_id]

    def connected(self, source: Source) -> bool:
        """Say whether the agent of an export holds a socket now."""
        with self.lock:
            return source.agent_id in self.connections

    def _connection(self, agent_id: str) -> Connection | None:
        with self.lock:
            return self.connections.get(agent_id)

    def _reconnected(self, agent_id: str) -> Connection:
        """Wait out the moment a rotation or a restart leaves the agent away."""
        deadline = time.monotonic() + RECONNECT_SECONDS
        while True:
            connection = self._connection(agent_id)
            if connection is not None and not connection.closed:
                return connection
            if time.monotonic() >= deadline:
                raise TreeUnavailable(f"agent {agent_id} is not connected")
            time.sleep(RECONNECT_POLL_SECONDS)

    def _run(self, source: Source, op: str, args: dict[str, Any]) -> Answer:
        """Ask from a worker thread, sending again after `busy` or a lost socket."""
        if on_event_loop():
            raise RuntimeError("a tree must not be asked from the event loop")
        seconds = OP_SECONDS.get(op, DEFAULT_SECONDS)
        lost_once = False
        busy = 0
        while True:
            connection = self._reconnected(source.agent_id)
            future = asyncio.run_coroutine_threadsafe(
                connection.request(op, args, seconds), connection.loop
            )
            try:
                return future.result()
            except ConnectionLost:
                if lost_once:
                    raise
                lost_once = True
            except TreeRefused as refused:
                busy += 1
                if refused.code != BUSY or busy >= BUSY_ATTEMPTS:
                    raise
                time.sleep(BUSY_PAUSE_SECONDS * busy)

    def call(self, source: Source, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run an operation answered with JSON."""
        return cast("dict[str, Any]", self._run(source, op, args))

    def data(self, source: Source, op: str, args: dict[str, Any]) -> bytes:
        """Run an operation answered with bytes."""
        return cast("bytes", self._run(source, op, args))

    def event(self, agent_id: str, export: str, paths: list[str] | None) -> None:
        """Tell the listeners, or remember it as a full pass when there are none."""
        with self.lock:
            listeners = list(self.listeners)
            if not listeners:
                self.owed.add((agent_id, export))
        for listener in listeners:
            listener(agent_id, export, paths)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Hear events from now on, starting with the full passes that are owed."""
        with self.lock:
            self.listeners.append(listener)
            owed = sorted(self.owed)
            self.owed.clear()
        for agent_id, export in owed:
            listener(agent_id, export, None)

        def unsubscribe() -> None:
            with self.lock:
                if listener in self.listeners:
                    self.listeners.remove(listener)

        return unsubscribe


HUB = Hub()
