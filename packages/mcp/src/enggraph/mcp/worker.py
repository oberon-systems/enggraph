"""Ask the worker API, which holds every tree, for what this server cannot read."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

WORKER_API_URL = os.environ.get("WORKER_API_URL", "").removesuffix("/")
WORKER_API_TOKEN = os.environ.get("WORKER_API_TOKEN", "")

RANGES_TIMEOUT = 10
GREP_TIMEOUT = 90
DROP_TIMEOUT = 600
MAX_RANGES = 200
URI_SAFE = "-_.!~*'()"


def worker_configured() -> bool:
    """Say whether there is a worker API to ask."""
    return WORKER_API_URL != "" and WORKER_API_TOKEN != ""


def post(path: str, body: Any, timeout: int) -> Any:  # noqa: ANN401
    """Send one request and return the parsed answer, or raise with its text."""
    request = urllib.request.Request(  # noqa: S310
        f"{WORKER_API_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {WORKER_API_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310
            return json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as refused:
        detail = refused.read().decode("utf-8", errors="replace")
        message = f"worker API {path}: {refused.code} {detail}"
        raise RuntimeError(message) from refused


def read_ranges(ranges: list[dict[str, Any]]) -> list[str | None]:
    """Return the text of each range, None where it cannot be read.

    Never raises: a packet or a search without its snippets is still an answer.
    """
    if not ranges or not worker_configured():
        return [None for _ in ranges]
    texts: list[str | None] = []
    try:
        for at in range(0, len(ranges), MAX_RANGES):
            batch = ranges[at : at + MAX_RANGES]
            body = post("/content/ranges", {"ranges": batch}, RANGES_TIMEOUT)
            answered = body["ranges"]
            for index in range(len(batch)):
                entry = answered[index] if index < len(answered) else None
                texts.append((entry or {}).get("text"))
    except Exception:  # noqa: BLE001 - an answer without snippets is an answer
        return [None for _ in ranges]
    return texts


def grep_trees(ask: dict[str, Any]) -> dict[str, Any]:
    """Return the lines of the mounted trees matching a pattern."""
    if not worker_configured():
        raise RuntimeError(
            "No worker API is configured: the trees are read through it, so a "
            "search over their text cannot run"
        )
    return post("/grep", ask, GREP_TIMEOUT)


def drop_project(project: str) -> None:
    """Delete a project and every row naming it."""
    if not worker_configured():
        raise RuntimeError(
            "No worker API is configured: a project is dropped through it, "
            "because it is what names every table the project has rows in"
        )
    name = urllib.parse.quote(project, safe=URI_SAFE)
    post(f"/projects/{name}/drop", {}, DROP_TIMEOUT)
