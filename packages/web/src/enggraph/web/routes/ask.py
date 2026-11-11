"""The Ask page's tools: listed, and called."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.web.args import (
    MISSING,
    Body,
    bad_request,
    field,
    read_body_string,
    read_query,
    reply,
    require_body_string,
)
from enggraph.web.guard import require_operator_when_enforced
from enggraph.web.mcpclient import call_tool, list_tools

router = APIRouter()


@router.get("/ask/tools")
async def _tools(request: Request) -> Response:
    require_operator_when_enforced()
    return reply({"tools": await list_tools(read_query(request, "project") or "")})


@router.post("/ask")
async def _ask(body: Body) -> Response:
    require_operator_when_enforced()
    tool = require_body_string(body, "tool")
    args = field(body, "arguments")
    if args is MISSING or args is None:
        args = {}
    if not isinstance(args, dict):
        raise bad_request('Field "arguments" must be an object')
    project = read_body_string(body, "project") or ""
    return reply(await call_tool(project, tool, args))
