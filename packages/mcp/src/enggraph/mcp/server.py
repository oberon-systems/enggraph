"""The MCP server over HTTP: sessions, the two transports and the guard.

One server object answers every session. What differs between sessions - the
project in the address, the skills it is told about, whether its first call
was answered yet - lives in a context variable set when the session opens,
which every task of that session inherits.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from enggraph.core import db, jsjson
from enggraph.mcp import handlers, tools
from enggraph.mcp.errors import message
from enggraph.mcp.scope import read_scope
from enggraph.mcp.skills import effective_skills, skill_instructions, with_skill_check
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

LOG = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

PORT = int(os.environ.get("PORT", "3000"))
# The project a client gets when it connects to a bare /mcp or /sse. Left
# unset, such a session has no default and every tool call has to name one.
DEFAULT_PROJECT = os.environ.get("DEFAULT_PROJECT")
VIEWER_URL = os.environ.get("VIEWER_URL", "http://localhost:3001/graph")
SESSION_HEADER = "mcp-session-id"
URI_SAFE = "-_.!~*'()"


def csv(value: str | None) -> list[str]:
    """Split a comma-separated setting into its entries."""
    return [entry.strip() for entry in (value or "").split(",") if entry.strip()]


ALLOWED_HOSTS = set(
    csv(os.environ.get("ALLOWED_HOSTS"))
    or [
        f"localhost:{PORT}",
        f"127.0.0.1:{PORT}",
        f"[::1]:{PORT}",
        "localhost",
        "127.0.0.1",
        "mcp-server:3000",
    ]
)
# Looked for among the entries: compose appends its own host to whatever
# GATEWAY_HOSTS holds, so the value is never the bare `*` written in .env.
ANY_HOST = "*" in ALLOWED_HOSTS
# Empty by default: an MCP client sends no Origin header at all, so a request
# that carries one is browser traffic and is refused.
ALLOWED_ORIGINS = set(csv(os.environ.get("ALLOWED_ORIGINS")))


@dataclass
class Session:
    """What one session keeps: its project, its skills, its first call."""

    project: str | None
    instructions: str
    skills_checked: bool = False


SESSION: ContextVar[Session | None] = ContextVar("enggraph_session", default=None)


def current() -> Session:
    """Return the session a request belongs to."""
    session = SESSION.get()
    if session is None:
        # A message handled outside any session we opened has no project.
        return Session(project=DEFAULT_PROJECT, instructions=skill_instructions(None))
    return session


def read_skills(project: str | None) -> str:
    """Return the skill instructions for a project, read fresh."""
    try:
        return skill_instructions(effective_skills(project))
    except Exception as error:  # noqa: BLE001 - a session opens without the list
        LOG.error("Skill lookup failed: %s", message(error))
        return skill_instructions(None)


def held_members(project: str | None) -> int | None:
    """Return how many projects a session's organization holds, if it is one."""
    if project is None:
        return None
    try:
        scope = read_scope(project)
    except Exception as error:  # noqa: BLE001 - the list is shown without the note
        LOG.error("Session project lookup failed: %s", message(error))
        return None
    return len(scope["members"]) if scope["organization"] else None


class SessionServer(Server[Any, Any]):
    """A server whose instructions are the ones of the session being opened."""

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        """Return the options, with this session's skills as instructions."""
        options = super().create_initialization_options(
            notification_options, experimental_capabilities
        )
        return options.model_copy(update={"instructions": current().instructions})


server = SessionServer("enggraph", version="1.0.0")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Return the tools, worded for the project of this session."""
    project = current().project
    members = await anyio.to_thread.run_sync(held_members, project)
    return [types.Tool(**tool) for tool in tools.list_tools(project, members)]


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Run one tool; the first answer of a session also lists its skills."""
    session = current()
    result = await anyio.to_thread.run_sync(
        handlers.call_tool, name, arguments, session.project
    )
    if not session.skills_checked and name not in handlers.SKILL_TOOLS:
        session.skills_checked = True
        check = await anyio.to_thread.run_sync(read_skills, session.project)
        result = with_skill_check(result, check)
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=one["text"])
            for one in result["content"]
        ],
        isError=bool(result.get("isError")),
    )


OPEN = TransportSecuritySettings(enable_dns_rebinding_protection=False)
sse = SseServerTransport("/message", security_settings=OPEN)
# Sessions live until the client closes them, as they always did.
manager = StreamableHTTPSessionManager(
    app=server, security_settings=OPEN, session_idle_timeout=None
)


async def open_session(project: str | None) -> None:
    """Bind the session about to start to its project and its skills."""
    instructions = await anyio.to_thread.run_sync(read_skills, project)
    SESSION.set(Session(project=project, instructions=instructions))


def refused(request: Request) -> Response | None:
    """Refuse a request whose Host or Origin a browser attack would carry."""
    if not ANY_HOST:
        host = request.headers.get("host")
        if host is None or host not in ALLOWED_HOSTS:
            return PlainTextResponse(f'Host "{host or ""}" is not allowed', 403)
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        return PlainTextResponse(f'Origin "{origin}" is not allowed', 403)
    return None


def open_sessions() -> int:
    """Count the sessions of both transports."""
    streams = getattr(sse, "_read_stream_writers", {})
    instances = getattr(manager, "_server_instances", {})
    return len(streams) + len(instances)


def project_names() -> list[str]:
    """Return every project name, which is what health reports."""
    return [row["name"] for row in db.query("SELECT name FROM projects ORDER BY name")]


async def health() -> Response:
    """Say whether the database answers, and name the projects in it."""
    try:
        names = await anyio.to_thread.run_sync(project_names)
    except Exception as error:  # noqa: BLE001 - reported as the answer
        body = jsjson.dumps({"status": "error", "error": message(error)})
        return Response(body, 503, media_type="application/json")
    body = jsjson.dumps(
        {"status": "ok", "sessions": open_sessions(), "projects": names}
    )
    return Response(body, media_type="application/json")


def graph_redirect(request: Request, project: str | None) -> Response:
    """Send a reader to the viewer, which draws the project it is given."""
    named = project if project is not None else request.query_params.get("project")
    suffix = f"?project={urllib.parse.quote(named, safe=URI_SAFE)}" if named else ""
    return RedirectResponse(f"{VIEWER_URL}{suffix}", 302)


async def handle_sse(
    scope: Scope, receive: Receive, send: Send, project: str | None
) -> None:
    """Open an SSE session and serve it until the client leaves."""
    await open_session(project)
    try:
        async with sse.connect_sse(scope, receive, send) as (read, write):
            await server.run(read, write, server.create_initialization_options())
    except Exception:
        LOG.exception("Failed to establish SSE session")


async def handle_streamable(
    scope: Scope, receive: Receive, send: Send, project: str | None
) -> None:
    """Serve one Streamable HTTP request, opening a session when it starts one."""
    request = Request(scope, receive)
    session_id = request.headers.get(SESSION_HEADER)
    if session_id is not None:
        if session_id not in getattr(manager, "_server_instances", {}):
            await PlainTextResponse("Unknown session", 404)(scope, receive, send)
            return
    elif request.method != "POST":
        # Only an initialize POST may arrive without a session id.
        answer = PlainTextResponse("Missing mcp-session-id header", 400)
        await answer(scope, receive, send)
        return
    else:
        await open_session(project)
    await manager.handle_request(scope, receive, send)


def route_project(parts: list[str]) -> str | None:
    """Read the project out of an address, falling back to the default."""
    return parts[1] if len(parts) > 1 and parts[1] != "" else DEFAULT_PROJECT


async def lifespan(receive: Receive, send: Send) -> None:
    """Hold the session manager open for as long as the server runs."""
    await receive()
    async with manager.run():
        await send({"type": "lifespan.startup.complete"})
        LOG.info("MCP Server running on port %d", PORT)
        await receive()
    await anyio.to_thread.run_sync(db.close)
    await send({"type": "lifespan.shutdown.complete"})


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    """Route one request to the transport or the page it names."""
    if scope["type"] == "lifespan":
        await lifespan(receive, send)
        return
    if scope["type"] != "http":
        return
    request = Request(scope, receive)
    parts = [part for part in scope["path"].split("/") if part != ""]
    head = parts[0] if parts else ""
    method = request.method
    answer: Response | None = None

    if head == "health" and len(parts) == 1 and method == "GET":
        answer = await health()
    elif head == "graph" and len(parts) <= 2 and method == "GET":
        answer = graph_redirect(request, parts[1] if len(parts) > 1 else None)
    elif head == "sse" and len(parts) <= 2 and method == "GET":
        answer = refused(request)
        if answer is None:
            await handle_sse(scope, receive, send, route_project(parts))
            return
    elif head == "message" and len(parts) == 1 and method == "POST":
        answer = refused(request)
        if answer is None:
            await sse.handle_post_message(scope, receive, send)
            return
    elif head == "mcp" and len(parts) <= 2 and method in ("POST", "GET", "DELETE"):
        answer = refused(request)
        if answer is None:
            await handle_streamable(scope, receive, send, route_project(parts))
            return
    else:
        answer = PlainTextResponse("Not Found", 404)
    await answer(scope, receive, send)
