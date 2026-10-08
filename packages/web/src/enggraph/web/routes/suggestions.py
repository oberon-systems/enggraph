"""Suggestions: listed, read, triaged and retired."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.core import db
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


def suggestions(
    scope: str | None,
    status: str | None,
    kind: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Page through the suggestions of a scope."""
    named, global_only = scoped(scope)
    rows = db.query(
        sql.SUGGESTIONS, [named, global_only, status, kind, pattern(q), limit, offset]
    )
    return listed(rows, "detail_length", limit, offset)


def facets() -> dict[str, Any]:
    """Return what the suggestions can be filtered by."""
    row = db.query(sql.SUGGESTION_FACETS)[0]
    return {
        "abouts": sorted(row["abouts"] or []),
        "statuses": sorted(row["statuses"] or []),
        "kinds": sorted(row["kinds"] or []),
        "global_suggestions": count(row["global_suggestions"]),
    }


def suggestion(suggestion_id: str) -> dict[str, Any]:
    """Return one suggestion in full."""
    rows = db.query(sql.SUGGESTION, [suggestion_id])
    if not rows:
        raise not_found(f'No suggestion "{suggestion_id}"')
    return rows[0]


def patch(suggestion_id: str, body: object) -> dict[str, Any]:
    """Triage a suggestion: its wording and lifecycle, never its counts."""
    rows = db.query(
        sql.PATCH_SUGGESTION,
        [
            suggestion_id,
            read_body_string(body, "title"),
            read_body_string(body, "summary"),
            read_body_string(body, "detail"),
            read_body_string(body, "status"),
            read_body_string(body, "kind"),
            read_body_string(body, "lever"),
        ],
    )
    if not rows:
        raise not_found(f'No suggestion "{suggestion_id}"')
    return rows[0]


def drop(suggestion_id: str) -> dict[str, Any]:
    """Delete a suggestion and what tied it to code."""
    rows = db.query(sql.DROP_SUGGESTION, [suggestion_id])
    if not rows:
        raise not_found(f'No suggestion "{suggestion_id}". Nothing was deleted.')
    return rows[0]


@router.get("/suggestions")
def _suggestions(request: Request) -> Response:
    scope = read_query(request, "about")
    limit = read_limit(request)
    offset = read_offset(request)
    return reply(
        suggestions(
            scope,
            read_query(request, "status"),
            read_query(request, "kind"),
            read_query(request, "q"),
            limit,
            offset,
        )
    )


@router.get("/suggestions/facets")
def _facets() -> Response:
    return reply(facets())


@router.get("/suggestion")
def _suggestion(request: Request) -> Response:
    return reply(suggestion(require_query(request, "id")))


@router.patch("/suggestion")
def _patch(request: Request, body: Body) -> Response:
    return reply(patch(require_query(request, "id"), body))


@router.delete("/suggestion")
def _drop(request: Request) -> Response:
    return reply(drop(require_query(request, "id")))
