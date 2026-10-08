"""Nodes, their neighbours and the files of a project."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.core import db
from enggraph.web import queries as sql
from enggraph.web.args import (
    bad_request,
    count,
    not_found,
    read_flag,
    read_limit,
    read_offset,
    read_query,
    reply,
    require_query,
)
from enggraph.web.routes.projects import require_project
from enggraph.web.upstream import file_text

# A one-character search matches most of a large graph and costs a full scan.
MIN_QUERY = 2
DIRECTIONS = ("in", "out", "both")

router = APIRouter()


def pattern(value: str | None) -> str | None:
    """Return the ILIKE pattern of a search, or None when it is too short."""
    if value is None or len(value.strip()) < MIN_QUERY:
        return None
    return f"%{value.strip()}%"


def paged(rows: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    """Shape one page of rows that each carry the window total."""
    return {
        "items": [{k: v for k, v in row.items() if k != "total"} for row in rows],
        "total": count(rows[0]["total"]) if rows else 0,
        "limit": limit,
        "offset": offset,
    }


def nodes(
    name: str,
    q: str | None,
    kind: str | None,
    file: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Page through the nodes of a project."""
    rows = db.query(sql.NODES, [name, pattern(q), kind, file, limit, offset])
    return paged(rows, limit, offset)


def node(name: str, node_id: str, content: bool) -> dict[str, Any]:
    """Describe one node, with its file text when asked for."""
    rows = db.query(sql.NODE, [name, node_id])
    if not rows:
        raise not_found(f'No node "{node_id}" in project "{name}"')
    row = rows[0]
    detail = {
        **row,
        "content_length": 0,
        "content": None,
        "content_truncated": False,
        "content_reason": None,
    }
    # Only a file has text, and it lives on the mount rather than in the graph.
    if content and row["type"] == "file" and row["file_path"]:
        read = file_text(name, row["file_path"])
        detail["content"] = read["content"]
        detail["content_length"] = read["chars"]
        detail["content_truncated"] = read["truncated"]
        detail["content_reason"] = read["reason"]
    return detail


def neighbors(
    name: str, node_id: str, direction: str, limit: int, offset: int
) -> dict[str, Any]:
    """Page through what a node points at and what points at it."""
    rows = db.query(sql.NEIGHBORS, [name, node_id, direction, limit, offset])
    return paged(rows, limit, offset)


def files(name: str, q: str | None, limit: int, offset: int) -> dict[str, Any]:
    """Page through the file nodes of a project."""
    page = paged(db.query(sql.FILES, [name, pattern(q), limit, offset]), limit, offset)
    for item in page["items"]:
        item["entities"] = count(item["entities"])
    return page


@router.get("/projects/{name}/nodes")
def _nodes(name: str, request: Request) -> Response:
    name = require_project(name)
    limit = read_limit(request)
    offset = read_offset(request)
    return reply(
        nodes(
            name,
            read_query(request, "q"),
            read_query(request, "type"),
            read_query(request, "file"),
            limit,
            offset,
        )
    )


@router.get("/projects/{name}/node")
def _node(name: str, request: Request) -> Response:
    name = require_project(name)
    node_id = require_query(request, "id")
    return reply(node(name, node_id, read_flag(request, "content")))


@router.get("/projects/{name}/neighbors")
def _neighbors(name: str, request: Request) -> Response:
    name = require_project(name)
    node_id = require_query(request, "id")
    direction = read_query(request, "direction") or "both"
    if direction not in DIRECTIONS:
        raise bad_request('Query parameter "direction" must be in, out or both')
    limit = read_limit(request)
    offset = read_offset(request)
    return reply(neighbors(name, node_id, direction, limit, offset))


@router.get("/projects/{name}/files")
def _files(name: str, request: Request) -> Response:
    name = require_project(name)
    limit = read_limit(request)
    offset = read_offset(request)
    return reply(files(name, read_query(request, "q"), limit, offset))
