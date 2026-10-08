"""Relations between projects, read and written through the MCP server."""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.web.args import (
    Body,
    bad_request,
    number,
    read_body_string,
    read_query,
    reply,
    require_body_string,
)
from enggraph.web.mcpclient import call_link_tool

router = APIRouter()


def _number(raw: str | None, fallback: int) -> float:
    value = fallback if raw is None else number(raw)
    return value if math.isfinite(value) else fallback


def _whole(value: float) -> float | int:
    return int(value) if value == int(value) else value


async def links(name: str, depth: str | None, direction: str | None) -> dict[str, Any]:
    """Return what a project is related to."""
    said = await call_link_tool(
        name,
        "get_project_links",
        {"depth": _whole(_number(depth, 1)), "direction": direction or "both"},
    )
    return json.loads(said)


async def trace(name: str, node: str | None, max_steps: str | None) -> dict[str, Any]:
    """Follow a node to what builds, deploys and runs it."""
    if node is None or node == "":
        raise bad_request("Name the node to trace from: ?node=<id>")
    said = await call_link_tool(
        name,
        "trace",
        {"node_id": node, "max_steps": _whole(_number(max_steps, 60))},
    )
    return json.loads(said)


def _relation(page: str, body: object) -> dict[str, Any]:
    # A declared relation is written under the project it leaves.
    source = read_body_string(body, "from")
    source = page if source is None else source
    target = require_body_string(body, "to")
    if page not in (source, target):
        raise bad_request(f"A relation shown here starts or ends at {page}")
    return {
        "project": source,
        "target_project": target,
        "relation": require_body_string(body, "relation"),
        "source_id": _or(read_body_string(body, "source_id"), "./"),
        "target_id": _or(read_body_string(body, "target_id"), "./"),
    }


def _or(value: str | None, fallback: str) -> str:
    return fallback if value is None else value


async def save_link(name: str, body: object) -> dict[str, str]:
    """Declare a relation between two projects."""
    args = _relation(name, body)
    note = read_body_string(body, "note")
    if note is not None:
        args["note"] = note
    return {"said": await call_link_tool(args["project"], "save_project_link", args)}


async def drop_link(name: str, body: object) -> dict[str, str]:
    """Take a declared relation back."""
    args = _relation(name, body)
    return {"said": await call_link_tool(args["project"], "drop_project_link", args)}


async def save_export(name: str, body: object) -> dict[str, str]:
    """Declare a name a project provides."""
    args = {
        "project": name,
        "kind": require_body_string(body, "kind"),
        "name": require_body_string(body, "name"),
        "node_id": _or(read_body_string(body, "node_id"), "./"),
    }
    return {"said": await call_link_tool(name, "save_project_export", args)}


async def drop_export(name: str, body: object) -> dict[str, str]:
    """Take a provided name back."""
    args = {
        "project": name,
        "kind": require_body_string(body, "kind"),
        "name": require_body_string(body, "name"),
    }
    return {"said": await call_link_tool(name, "drop_project_export", args)}


@router.get("/projects/{name}/links")
async def _links(name: str, request: Request) -> Response:
    depth = read_query(request, "depth")
    return reply(await links(name, depth, read_query(request, "direction")))


@router.get("/projects/{name}/trace")
async def _trace(name: str, request: Request) -> Response:
    node = read_query(request, "node")
    return reply(await trace(name, node, read_query(request, "max_steps")))


@router.post("/projects/{name}/links")
async def _save_link(name: str, body: Body) -> Response:
    return reply(await save_link(name, body), 201)


@router.delete("/projects/{name}/links")
async def _drop_link(name: str, body: Body) -> Response:
    return reply(await drop_link(name, body))


@router.post("/projects/{name}/exports")
async def _save_export(name: str, body: Body) -> Response:
    return reply(await save_export(name, body), 201)


@router.delete("/projects/{name}/exports")
async def _drop_export(name: str, body: Body) -> Response:
    return reply(await drop_export(name, body))
