"""The dashboard: a JSON API over the schema, and the pages that read it."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.staticfiles import StaticFiles

from enggraph.core import db, failures
from enggraph.web import auth, mcpclient, pages, proxy
from enggraph.web.args import HttpError, reply
from enggraph.web.guard import PRINCIPAL, guard, principal_of
from enggraph.web.routes import (
    agents,
    ask,
    issues,
    links,
    memories,
    nodes,
    plans,
    projects,
    prompts,
    records,
    roadmaps,
    settings,
    skills,
    suggestions,
    tokens,
)

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
# The image carries htmx beside the package, as the viewer carries its library.
HTMX_PATH = os.environ.get("HTMX_PATH", str(HERE / "static" / "htmx.min.js"))
Next = Callable[[Request], Awaitable[Response]]
EVERY_METHOD = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Let go of the sessions and the connections when the server stops."""
    yield
    await mcpclient.close_sessions()
    await proxy.close()
    db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
api = APIRouter(prefix="/api", dependencies=[Depends(guard)])


@api.get("/health")
def health() -> Response:
    """Answer once the database does."""
    db.query("SELECT 1")
    return reply({"status": "ok", "db": "ok"})


# The order the routes are matched in: a records path before the one it shadows.
for part in (
    ask,
    links,
    projects,
    nodes,
    plans,
    prompts,
    roadmaps,
    memories,
    records,
    suggestions,
    issues,
    settings,
    skills,
    tokens,
    agents,
):
    api.include_router(part.router)


@api.api_route("/{rest:path}", methods=EVERY_METHOD)
def no_such_endpoint(rest: str) -> Response:
    """Answer an address the API does not have."""
    del rest
    return reply({"error": "No such endpoint"}, 404)


app.include_router(api)


def _api(request: Request) -> bool:
    return request.url.path.startswith("/api/") or request.url.path == "/api"


@app.exception_handler(HttpError)
async def refused(request: Request, error: HttpError) -> Response:
    """Answer an error a route raised, with the status it named."""
    return refused_as(request, error)


def refused_as(request: Request, error: HttpError) -> Response:
    """Say an error as JSON to the API and as a page to a browser."""
    if _api(request):
        return reply({"error": error.message}, error.status)
    return pages.failed(error)


# A middleware and not a handler for Exception: that one answers and then
# lets the error through, and the server drops the connection it came on.
@app.middleware("http")
async def broke(request: Request, call_next: Next) -> Response:
    """Answer anything else as the service being unable to, and log it."""
    try:
        return await call_next(request)
    except Exception as error:  # noqa: BLE001 - every failure is an answer
        if failures.nul(error):
            return refused_as(request, HttpError(400, failures.NUL))
        code = "the dashboard could not answer"
        error_id = failures.note(log, code, error)
        if _api(request):
            return reply({"error": code, "error_id": error_id}, 503)
        return pages.failed(HttpError(503, failures.told(code, error_id)))


@app.middleware("http")
async def identify(request: Request, call_next: Next) -> Response:
    """Hold who the gateway said the request is from, for the routes below."""
    PRINCIPAL.set(principal_of(request))
    return await call_next(request)


app.include_router(auth.router)

for viewer_route in proxy.VIEWER_ROUTES:
    app.add_api_route(viewer_route, proxy.proxy_viewer, methods=["GET"])


@app.get("/static/htmx.min.js")
def htmx() -> Response:
    """Serve the library the pages refresh themselves with."""
    if not Path(HTMX_PATH).is_file():
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(HTMX_PATH, media_type="text/javascript")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# A probe for something this stack does not serve must fail as a 404 and not
# as a page: an MCP client checking for OAuth broke on parsing HTML.
@app.get("/.well-known/{rest:path}")
def well_known(rest: str) -> Response:
    """Refuse a probe for what this stack does not serve."""
    del rest
    return PlainTextResponse("Not found", status_code=404)


app.include_router(pages.router)


@app.get("/{rest:path}")
def no_such_page(rest: str) -> Response:
    """Answer an address no page lives at."""
    del rest
    return pages.no_such_page()
