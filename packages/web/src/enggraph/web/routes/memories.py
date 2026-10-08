"""Memories: listed, read, corrected and retired."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.core import db, jsjson
from enggraph.web import queries as sql
from enggraph.web.args import (
    Body,
    count,
    not_found,
    read_body_string,
    read_limit,
    read_offset,
    read_query,
    reply,
    require_query,
)
from enggraph.web.routes.nodes import pattern
from enggraph.web.routes.plans import listed, scoped

router = APIRouter()


def memories(
    scope: str | None, tagged: str | None, q: str | None, limit: int, offset: int
) -> dict[str, Any]:
    """Page through the memories of a scope."""
    named, global_only = scoped(scope)
    rows = db.query(
        sql.MEMORIES, [named, global_only, tagged, pattern(q), limit, offset]
    )
    return listed(rows, "text_length", limit, offset)


def facets() -> dict[str, Any]:
    """Return what the memories can be filtered by."""
    row = db.query(sql.MEMORY_FACETS)[0]
    return {
        "abouts": sorted(row["abouts"] or []),
        "tags": sorted(row["tags"] or []),
        "global_memories": count(row["global_memories"]),
    }


def memory(memory_id: str) -> dict[str, Any]:
    """Return one memory in full."""
    rows = db.query(sql.MEMORY, [memory_id])
    if not rows:
        raise not_found(f'No memory "{memory_id}"')
    return rows[0]


def patch(memory_id: str, body: object) -> dict[str, Any]:
    """Correct a memory; its scope and slug are its id and stay."""
    tags = read_body_string(body, "tags")
    kept = None
    if tags is not None:
        kept = jsjson.dumps([tag.strip() for tag in tags.split(",") if tag.strip()])
    rows = db.query(
        sql.PATCH_MEMORY,
        [
            memory_id,
            read_body_string(body, "title"),
            read_body_string(body, "summary"),
            read_body_string(body, "text"),
            kept,
        ],
    )
    if not rows:
        raise not_found(f'No memory "{memory_id}"')
    return rows[0]


def drop(memory_id: str) -> dict[str, Any]:
    """Delete a memory and what tied it to code."""
    rows = db.query(sql.DROP_MEMORY, [memory_id])
    if not rows:
        raise not_found(f'No memory "{memory_id}". Nothing was deleted.')
    return rows[0]


@router.get("/memories")
def _memories(request: Request) -> Response:
    scope = read_query(request, "about")
    limit = read_limit(request)
    offset = read_offset(request)
    return reply(
        memories(
            scope,
            read_query(request, "tag"),
            read_query(request, "q"),
            limit,
            offset,
        )
    )


@router.get("/memories/facets")
def _facets() -> Response:
    return reply(facets())


@router.get("/memory")
def _memory(request: Request) -> Response:
    return reply(memory(require_query(request, "id")))


@router.patch("/memory")
def _patch(request: Request, body: Body) -> Response:
    return reply(patch(require_query(request, "id"), body))


@router.delete("/memory")
def _drop(request: Request) -> Response:
    return reply(drop(require_query(request, "id")))
