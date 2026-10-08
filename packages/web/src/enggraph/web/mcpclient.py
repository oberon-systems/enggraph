"""Call the MCP server's tools, as one more client of it."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

from mcp import ClientSession, McpError
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from enggraph.core import jsjson
from enggraph.web.args import HttpError, bad_request
from enggraph.web.upstream import segment

MCP_URL = os.environ.get("MCP_URL", "http://mcp-server:3000")
CALL_TIMEOUT = timedelta(seconds=180)
TIMED_OUT = 408
# What the SDK answers when the server no longer knows the session.
SESSION_GONE = "Session terminated"

ASK_GROUPS: list[tuple[str, list[str]]] = [
    ("Context", ["get_context", "search_code", "search_text", "get_overview"]),
    (
        "Graph",
        [
            "search_code_nodes",
            "get_code_graph_neighbors",
            "get_node_summary",
            "shortest_path",
        ],
    ),
    (
        "Symbols",
        [
            "find_definition",
            "find_callers",
            "find_callees",
            "find_references",
            "find_implementations",
            "find_tests",
            "impact_analysis",
        ],
    ),
    (
        "Project",
        [
            "describe_project",
            "get_project_links",
            "find_linked_name",
            "list_projects",
            "list_indexed_files",
        ],
    ),
    ("Records", ["get_plans", "get_memory", "get_suggestions"]),
]
OFFERED = {name for _, names in ASK_GROUPS for name in names}

# The links tab reads and writes through the MCP server, which owns the rules
# a name is normalized and a relation is checked by.
LINK_TOOLS = {
    "get_project_links",
    "save_project_link",
    "drop_project_link",
    "save_project_export",
    "drop_project_export",
    "trace",
}

T = TypeVar("T")


class _Held:
    """One open session, kept by the task that opened it."""

    def __init__(self, project: str) -> None:
        """Start opening a session for a project."""
        self.ready: asyncio.Future[ClientSession] = (
            asyncio.get_running_loop().create_future()
        )
        self.release = asyncio.Event()
        self.task = asyncio.create_task(self._hold(project))

    async def _hold(self, project: str) -> None:
        path = "/mcp" if project == "" else f"/mcp/{segment(project)}"
        info = Implementation(name="enggraph-dashboard", version="1.0.0")
        try:
            async with (
                streamablehttp_client(MCP_URL.rstrip("/") + path) as (read, write, _),
                ClientSession(read, write, client_info=info) as session,
            ):
                await session.initialize()
                self.ready.set_result(session)
                await self.release.wait()
        except BaseException as error:
            # A caller waiting on the session is told; after that it is just gone.
            if not self.ready.done():
                self.ready.set_exception(ConnectionError(_reason(error)))
            if isinstance(error, asyncio.CancelledError):
                raise


_sessions: dict[str, _Held] = {}


def _session(project: str) -> _Held:
    held = _sessions.get(project)
    if held is None or held.task.done():
        held = _Held(project)
        _sessions[project] = held
    return held


def _reason(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return _reason(error.exceptions[0])
    return str(error) or type(error).__name__


def _pass_on(error: BaseException) -> HttpError:
    if isinstance(error, McpError):
        code = error.error.code
        status = 504 if code == TIMED_OUT else 502
        return HttpError(status, f"MCP error {code}: {error.error.message}")
    return HttpError(
        502, f"the MCP server at {MCP_URL} did not answer: {_reason(error)}"
    )


def _gone(error: BaseException) -> bool:
    if isinstance(error, McpError):
        return error.error.message == SESSION_GONE
    return True


async def _with_session(
    project: str, run: Callable[[ClientSession], Awaitable[T]]
) -> T:
    """Run a call on the project's session, opening it once more when gone."""
    for last_try in (False, True):
        held = _session(project)
        try:
            return await run(await held.ready)
        except Exception as error:  # noqa: BLE001 - every failure is passed on
            if last_try or not _gone(error):
                raise _pass_on(error) from error
            if _sessions.get(project) is held:
                del _sessions[project]
            held.release.set()
    raise HttpError(502, f"the MCP server at {MCP_URL} did not answer")


async def list_tools(project: str) -> list[dict[str, Any]]:
    """Return the tools the Ask page offers, in its groups."""
    listed = await _with_session(project, lambda session: session.list_tools())
    by_name = {tool.name: tool for tool in listed.tools}
    return [
        {
            "name": name,
            "group": group,
            "description": by_name[name].description or "",
            "inputSchema": by_name[name].inputSchema,
        }
        for group, names in ASK_GROUPS
        for name in names
        if name in by_name
    ]


def _blocks(content: object) -> list[dict[str, str]]:
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        said = getattr(block, "text", None)
        if isinstance(said, str):
            out.append({"type": "text", "text": said})
        else:
            dumped = block.model_dump(mode="json", exclude_none=True)
            out.append({"type": str(dumped.get("type")), "text": jsjson.dumps(dumped)})
    return out


def _units(value: str) -> int:
    """Count a string the way JavaScript's length does."""
    return len(value.encode("utf-16-le")) // 2


async def _call(project: str, name: str, args: dict[str, Any]) -> Any:  # noqa: ANN401
    return await _with_session(
        project,
        lambda session: session.call_tool(
            name, args, read_timeout_seconds=CALL_TIMEOUT
        ),
    )


async def call_tool(project: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call one of the offered tools and time it."""
    if name not in OFFERED:
        raise bad_request(f'Tool "{name}" is not offered here')
    started = time.monotonic()
    result = await _call(project, name, args)
    content = _blocks(result.content)
    return {
        "content": content,
        "is_error": result.isError is True,
        "ms": round((time.monotonic() - started) * 1000),
        "chars": sum(_units(block["text"]) for block in content),
    }


async def call_link_tool(project: str, name: str, args: dict[str, Any]) -> str:
    """Call one of the link tools and return what it said."""
    if name not in LINK_TOOLS:
        raise bad_request(f'Tool "{name}" is not a link tool')
    result = await _call(project, name, args)
    # The first call of a session gets the skill check appended as a later block.
    content = _blocks(result.content)
    said = content[0]["text"] if content else ""
    if result.isError is True:
        raise bad_request(said)
    return said


async def close_sessions() -> None:
    """Let every session go, when the server stops."""
    held = list(_sessions.values())
    _sessions.clear()
    for one in held:
        one.release.set()
    await asyncio.gather(*(one.task for one in held), return_exceptions=True)
