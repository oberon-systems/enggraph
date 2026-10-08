"""Ask the worker API, which owns the mounts, the queues and the schedule."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from enggraph.web.args import API_TOKEN, API_URL, URI_SAFE, HttpError

# Whole file bodies: a page of them would be megabytes, so this much and no more.
MAX_CONTENT = 100_000
NO_HURRY = 300.0

_client = httpx.Client()


class UpstreamError(Exception):
    """A failure of the API, with the status to pass on."""

    def __init__(self, status: int, message: str) -> None:
        """Keep the status beside the message."""
        super().__init__(message)
        self.status = status
        self.message = message


def segment(value: str) -> str:
    """Escape one path segment as encodeURIComponent does."""
    return quote(value, safe=URI_SAFE)


def call(
    method: str,
    path: str,
    params: dict[str, str] | None = None,
    body: object = None,
    timeout_ms: int | None = None,
) -> Any:  # noqa: ANN401
    """Call the API, and turn its failures into ones this service can pass on."""
    timeout = NO_HURRY if timeout_ms is None else timeout_ms / 1000
    try:
        response = _client.request(
            method,
            API_URL.rstrip("/") + path,
            params=params or None,
            headers={
                "Authorization": f"Bearer {API_TOKEN}",
                "Content-Type": "application/json",
            },
            content=None if body is None else json.dumps(body),
            timeout=timeout,
        )
    except httpx.TimeoutException as error:
        if timeout_ms is None:
            raise UpstreamError(
                502, f"the API is not reachable at {API_URL}"
            ) from error
        raise UpstreamError(
            504, f"the API did not answer within {timeout_ms} ms"
        ) from error
    except httpx.HTTPError as error:
        raise UpstreamError(502, f"the API is not reachable at {API_URL}") from error
    parsed = None if response.text == "" else json.loads(response.text)
    if not response.is_success:
        detail = parsed.get("detail") if isinstance(parsed, dict) else None
        raise UpstreamError(
            response.status_code,
            detail or f"the API answered {response.status_code}",
        )
    return parsed


def ask(
    method: str,
    path: str,
    params: dict[str, str] | None = None,
    body: object = None,
    timeout_ms: int | None = None,
) -> Any:  # noqa: ANN401
    """Call the API and pass a failure on with its own status."""
    try:
        return call(method, path, params, body, timeout_ms)
    except UpstreamError as error:
        raise HttpError(error.status, error.message) from error


def file_text(project: str, path: str) -> dict[str, Any]:
    """Read one file of an indexed tree, through the API that owns the mounts."""
    try:
        body = call(
            "GET",
            "/content",
            {"project": project, "path": path, "limit": str(MAX_CONTENT + 1)},
        )
        content = body["content"]
    except UpstreamError as error:
        return _unread(error.message)
    except (ValueError, KeyError, TypeError):
        return _unread("the API call failed")
    truncated = len(content) > MAX_CONTENT
    return {
        "content": content[:MAX_CONTENT] if truncated else content,
        "chars": MAX_CONTENT if truncated else len(content),
        "truncated": truncated,
        "reason": None,
    }


def _unread(reason: str) -> dict[str, Any]:
    return {"content": None, "chars": 0, "truncated": False, "reason": reason}
