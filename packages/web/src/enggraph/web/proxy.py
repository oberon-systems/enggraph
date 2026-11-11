"""Pass the graph page through, so it and the API share one origin."""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import Request
from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse, Response, StreamingResponse

from enggraph.core import failures

log = logging.getLogger(__name__)
VIEWER_SECONDS = float(os.environ.get("VIEWER_TIMEOUT_SECONDS", "120"))

VIEWER_URL = os.environ.get("VIEWER_URL", "http://viewer:3001")
# The viewer points the drawing library's script tag at this origin, so the
# page without the library is a blank frame.
VIEWER_ROUTES = ("/graph", "/vis-network.min.js")
PASSED = ("content-type", "cache-control")

# A graph takes a while to lay out; a viewer that never answers must not hold a request.
_client = httpx.AsyncClient(follow_redirects=True, timeout=VIEWER_SECONDS)


async def proxy_viewer(request: Request) -> Response:
    """Answer with what the viewer answers for the same path."""
    project = request.query_params.get("project", "")
    params = {"project": project} if project != "" else None
    target = VIEWER_URL.rstrip("/") + request.url.path
    try:
        upstream = await _client.send(
            _client.build_request("GET", target, params=params), stream=True
        )
    except httpx.HTTPError as error:
        code = "the viewer service does not answer"
        return PlainTextResponse(failures.hidden(log, code, error), status_code=502)
    headers = {
        name: upstream.headers[name] for name in PASSED if name in upstream.headers
    }
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(upstream.aclose),
    )


async def close() -> None:
    """Close the client, when the server stops."""
    await _client.aclose()
