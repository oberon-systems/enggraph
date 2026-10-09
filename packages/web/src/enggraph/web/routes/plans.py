"""Plans: listed, read, written and retired."""

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
    require_body_string,
    require_query,
)
from enggraph.web.routes.nodes import pattern

# The project is a free-text tag, so these two are selections rather than
# names: every project, and the records tagged with none.
ALL = "*"
GLOBAL_ONLY = "_global"

router = APIRouter()


def scoped(scope: str | None) -> tuple[str | None, bool]:
    """Split a scope into the tag to match and the global-only switch."""
    named = None if scope in (None, ALL, GLOBAL_ONLY) else scope
    return named, scope == GLOBAL_ONLY


def tag(value: str | None) -> str | None:
    """Return a plan's project as it is stored: a name, or None when global."""
    if value is None or value.strip() == "" or value == ALL:
        return None
    return value


def listed(
    rows: list[dict[str, Any]], length: str, limit: int, offset: int
) -> dict[str, Any]:
    """Shape one page of records, with the named length as a number."""
    items = [
        {**{k: v for k, v in row.items() if k != "total"}, length: count(row[length])}
        for row in rows
    ]
    return {
        "items": items,
        "total": count(rows[0]["total"]) if rows else 0,
        "limit": limit,
        "offset": offset,
    }


def plans(
    scope: str | None,
    status: str | None,
    kind: str | None,
    q: str | None,
    limit: int,
    offset: int,
    with_global: bool = False,
) -> dict[str, Any]:
    """Page through the plans of a scope, the global ones after them when asked."""
    named, global_only = scoped(scope)
    rows = db.query(
        sql.PLANS,
        [named, global_only, status, kind, pattern(q), limit, offset, with_global],
    )
    return listed(rows, "content_length", limit, offset)


def facets() -> dict[str, Any]:
    """Return what the plans can be filtered by, and where one can be filed."""
    row = db.query(sql.PLAN_FACETS)[0]
    return {
        "projects": sorted(row["projects"] or []),
        "statuses": sorted(row["statuses"] or []),
        "types": sorted(row["types"] or []),
        "global_plans": count(row["global_plans"]),
        "targets": db.query(sql.PLAN_TARGETS),
    }


def plan(plan_id: str) -> dict[str, Any]:
    """Return one plan in full."""
    rows = db.query(sql.PLAN, [plan_id])
    if not rows:
        raise not_found(f'No plan "{plan_id}"')
    return rows[0]


def save(body: object) -> dict[str, Any]:
    """Write a plan, new or over the one of the same id."""
    plan_id = require_body_string(body, "id")
    db.query(sql.ENSURE_PLANS_PROJECT)
    status = read_body_string(body, "status")
    kind = read_body_string(body, "type")
    about = tag(read_body_string(body, "project"))
    with db.transaction() as client:
        rows = client.query(
            sql.SAVE_PLAN,
            [
                plan_id,
                about,
                require_body_string(body, "title"),
                require_body_string(body, "content"),
                "active" if status is None else status,
                "plan" if kind is None else kind,
            ],
        )
        client.query(sql.MOVE_PLAN_PROMPTS, [plan_id, about])
    return {"id": plan_id, "created": rows[0]["created"]}


def patch(plan_id: str, body: object) -> dict[str, Any]:
    """Change the named fields of a plan."""
    # The project's null is a value, so whether it was named travels with it.
    project = read_body_string(body, "project")
    with db.transaction() as client:
        rows = client.query(
            sql.PATCH_PLAN,
            [
                plan_id,
                read_body_string(body, "title"),
                read_body_string(body, "content"),
                read_body_string(body, "status"),
                read_body_string(body, "type"),
                project is not None,
                tag(project),
            ],
        )
        if rows and project is not None:
            client.query(sql.MOVE_PLAN_PROMPTS, [plan_id, tag(project)])
    if not rows:
        raise not_found(f'No plan "{plan_id}"')
    return rows[0]


def drop(plan_id: str) -> dict[str, Any]:
    """Delete a plan, what tied it to code and its prompts; untie its items."""
    with db.transaction() as client:
        rows = client.query(sql.DROP_PLAN, [plan_id])
        # In a statement of its own, after the plan: see the `database` skill.
        if rows:
            client.query(sql.DROP_PLAN_PROMPTS, [plan_id])
            client.query(sql.DROP_PLAN_ITEMS, [plan_id])
    if not rows:
        raise not_found(f'No plan "{plan_id}". Nothing was deleted.')
    return rows[0]


@router.get("/plans")
def _plans(request: Request) -> Response:
    scope = read_query(request, "project")
    limit = read_limit(request)
    offset = read_offset(request)
    return reply(
        plans(
            scope,
            read_query(request, "status"),
            read_query(request, "type"),
            read_query(request, "q"),
            limit,
            offset,
        )
    )


@router.get("/plans/facets")
def _facets() -> Response:
    return reply(facets())


@router.get("/plan")
def _plan(request: Request) -> Response:
    return reply(plan(require_query(request, "id")))


@router.post("/plans")
def _save(body: Body) -> Response:
    saved = save(body)
    return reply(saved, 201 if saved["created"] else 200)


@router.patch("/plan")
def _patch(request: Request, body: Body) -> Response:
    return reply(patch(require_query(request, "id"), body))


@router.delete("/plan")
def _drop(request: Request) -> Response:
    return reply(drop(require_query(request, "id")))
