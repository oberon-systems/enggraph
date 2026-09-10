"""One JSON request to a model server, each phase under a timeout of its own."""

from __future__ import annotations

import http.client
import io
import json
import socket
import ssl
import time
import urllib.error
from typing import Any
from urllib.parse import urlsplit

from enggraph.config import Timeouts

READ_CHUNK_BYTES = 65536


class Unreachable(ConnectionError):
    """No connection was made: refused, unresolvable, or connect timed out."""


class TotalTimeout(TimeoutError):
    """The request as a whole ran past its total, whatever phase it was in."""


def _budget(deadline: float, phase: float, url: str, what: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TotalTimeout(f"{url}: total timeout reached during {what}")
    return min(phase, left)


def _arm(
    sock: socket.socket, deadline: float, limit: float, url: str, phase: str
) -> bool:
    """Set the timeout for one phase; True when the total is what bounds it."""
    budget = _budget(deadline, limit, url, phase)
    sock.settimeout(budget)
    return budget < limit


def _timed_out(
    error: TimeoutError, clipped: bool, url: str, phase: str, limit: float
) -> TimeoutError:
    if isinstance(error, TotalTimeout) or clipped:
        return TotalTimeout(f"{url}: total timeout reached during {phase}")
    return TimeoutError(f"{url}: {phase} timeout after {limit:g}s")


def _connection(
    url: str, timeouts: Timeouts, deadline: float
) -> tuple[http.client.HTTPConnection, socket.socket, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Unreachable(f"{url} is not an http or https URL")
    timeout = _budget(deadline, timeouts.connect, url, "connect")
    conn: http.client.HTTPConnection
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(
            parts.hostname,
            parts.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.connect()
    except OSError as error:
        conn.close()
        raise Unreachable(f"{url} did not accept a connection ({error})") from None
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return conn, conn.sock, path


def request_json(
    url: str, body: dict | None, timeouts: Timeouts, key: str = ""
) -> dict[str, Any]:
    """POST `body` (GET when None) and return the JSON answer.

    Raises Unreachable when no connection was made, TimeoutError naming the
    phase that ran out, and urllib's HTTPError for a status of 400 or more.
    """
    deadline = time.monotonic() + timeouts.total
    conn, sock, path = _connection(url, timeouts, deadline)
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    clipped = False
    try:
        try:
            clipped = _arm(sock, deadline, timeouts.write, url, "write")
            conn.request(
                "GET" if data is None else "POST", path, body=data, headers=headers
            )
        except TimeoutError as error:
            raise _timed_out(error, clipped, url, "write", timeouts.write) from None
        try:
            clipped = _arm(sock, deadline, timeouts.read, url, "read")
            response = conn.getresponse()
            chunks: list[bytes] = []
            # The response closes the socket once the body is read, so the
            # timeout is only reset while it is still open.
            while not response.isclosed():
                clipped = _arm(sock, deadline, timeouts.read, url, "read")
                chunk = response.read1(READ_CHUNK_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
        except TimeoutError as error:
            raise _timed_out(error, clipped, url, "read", timeouts.read) from None
        except http.client.HTTPException as error:
            raise ConnectionError(f"{url} broke off the answer ({error})") from None
    finally:
        conn.close()
    payload = b"".join(chunks)
    if response.status >= 400:
        raise urllib.error.HTTPError(
            url, response.status, response.reason, response.msg, io.BytesIO(payload)
        )
    return json.loads(payload.decode("utf-8"))
