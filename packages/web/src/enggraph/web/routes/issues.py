"""Issues: listed and read here, written through the MCP server."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.core import db
from enggraph.web import queries as sql
from enggraph.web.args import (
    Body,
    bad_request,
    not_found,
    read_body_string,
    read_limit,
    read_offset,
    read_query,
    reply,
    require_body_string,
    require_query,
)
from enggraph.web.mcpclient import call_issue_tool
from enggraph.web.routes.nodes import pattern
from enggraph.web.routes.plans import GLOBAL_ONLY, listed, scoped, tag
from enggraph.web.routes.prompts import of_plan

KINDS = ("bug", "security", "feature", "improvement", "debt")
STATUSES = ("open", "closed", "declined")
# What a change may carry; an empty commit, plan or resolution clears it.
FIELDS = (
    "title",
    "summary",
    "detail",
    "kind",
    "status",
    "commit",
    "plan_id",
    "resolution",
)

router = APIRouter()


def issues(
    scope: str | None,
    status: str | None,
    kind: str | None,
    q: str | None,
    limit: int,
    offset: int,
    with_global: bool = False,
) -> dict[str, Any]:
    """Page through the issues of a scope, the global ones after them when asked."""
    named, global_only = scoped(scope)
    rows = db.query(
        sql.ISSUES,
        [named, global_only, status, kind, pattern(q), limit, offset, with_global],
    )
    return listed(rows, "detail_length", limit, offset)


def issue(issue_id: str) -> dict[str, Any]:
    """Return one issue in full, with the plan that carries it."""
    rows = db.query(sql.ISSUE, [issue_id])
    if not rows:
        raise not_found(f'No issue "{issue_id}"')
    row = rows[0]
    plan = None
    if row["plan_id"] is not None:
        found = db.query(sql.ISSUE_PLAN, [row["plan_id"]])
        if found:
            plan = {**found[0], "prompts": of_plan(row["plan_id"])}
    return {**row, "plan": plan}


def split(issue_id: str) -> tuple[str, str]:
    """Return the scope an id names, as the tools take it, and its slug."""
    about, _, slug = issue_id.partition("/")
    if not slug:
        raise bad_request(f'"{issue_id}" is not an issue id: <about>/<slug>')
    return ("*" if about == GLOBAL_ONLY else about), slug


def changes(body: object) -> dict[str, str]:
    """Return the fields a body carries, as save_issue takes them."""
    args: dict[str, str] = {}
    for key in FIELDS:
        value = read_body_string(body, key)
        if value is not None:
            args[key] = value
    return args


async def save(body: object) -> dict[str, Any]:
    """Open an issue."""
    slug = require_body_string(body, "slug").strip()
    about = tag(read_body_string(body, "about"))
    args = {
        **changes(body),
        "issue_id": slug,
        "about": "*" if about is None else about,
    }
    said = await call_issue_tool("save_issue", args)
    return {"id": f"{GLOBAL_ONLY if about is None else about}/{slug}", "said": said}


async def patch(issue_id: str, body: object) -> dict[str, Any]:
    """Change the fields given of an issue: wording, lifecycle, how it was settled."""
    about, slug = split(issue_id)
    if not db.query(sql.ISSUE, [issue_id]):
        raise not_found(f'No issue "{issue_id}"')
    args = {**changes(body), "issue_id": slug, "about": about}
    return {"id": issue_id, "said": await call_issue_tool("save_issue", args)}


async def drop(issue_id: str) -> dict[str, Any]:
    """Delete an issue and what tied it to code."""
    about, slug = split(issue_id)
    said = await call_issue_tool("drop_issue", {"issue_id": slug, "about": about})
    return {"id": issue_id, "said": said}


@router.get("/issues")
def _issues(request: Request) -> Response:
    return reply(
        issues(
            read_query(request, "about"),
            read_query(request, "status"),
            read_query(request, "kind"),
            read_query(request, "q"),
            read_limit(request),
            read_offset(request),
        )
    )


@router.post("/issues")
async def _save(body: Body) -> Response:
    return reply(await save(body), 201)


@router.get("/issue")
def _issue(request: Request) -> Response:
    return reply(issue(require_query(request, "id")))


@router.patch("/issue")
async def _patch(request: Request, body: Body) -> Response:
    return reply(await patch(require_query(request, "id"), body))


@router.delete("/issue")
async def _drop(request: Request) -> Response:
    return reply(await drop(require_query(request, "id")))
