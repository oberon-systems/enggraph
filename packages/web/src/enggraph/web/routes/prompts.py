"""Prompts: the execution prompt of a plan, listed, read, written and dropped."""

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
from enggraph.web.routes.plans import listed, scoped

router = APIRouter()


def prompts(
    scope: str | None,
    status: str | None,
    plan_id: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Page through the prompts of a scope, or of one plan."""
    named, global_only = scoped(scope)
    rows = db.query(
        sql.PROMPTS, [named, global_only, status, plan_id, pattern(q), limit, offset]
    )
    return listed(rows, "content_length", limit, offset)


def facets() -> dict[str, Any]:
    """Return what the prompts can be filtered by."""
    row = db.query(sql.PROMPT_FACETS)[0]
    return {
        "projects": sorted(row["projects"] or []),
        "statuses": sorted(row["statuses"] or []),
        "global_prompts": count(row["global_prompts"]),
    }


def prompt(prompt_id: str) -> dict[str, Any]:
    """Return one prompt in full, with the title and status of its plan."""
    rows = db.query(sql.PROMPT, [prompt_id])
    if not rows:
        raise not_found(f'No prompt "{prompt_id}"')
    return rows[0]


def of_plan(plan_id: str) -> list[dict[str, Any]]:
    """Return the prompts of one plan, newest first."""
    return db.query(sql.PLAN_PROMPTS, [plan_id])


def save(body: object) -> dict[str, Any]:
    """Write a prompt under the scope of the plan it executes."""
    prompt_id = require_body_string(body, "id")
    plan_id = require_body_string(body, "plan_id")
    status = read_body_string(body, "status")
    rows = db.query(
        sql.SAVE_PROMPT,
        [
            prompt_id,
            plan_id,
            require_body_string(body, "title"),
            require_body_string(body, "content"),
            "active" if status is None else status,
        ],
    )
    if not rows:
        raise not_found(f'No plan "{plan_id}". A prompt executes a plan.')
    return {"id": prompt_id, "created": rows[0]["created"]}


def patch(prompt_id: str, body: object) -> dict[str, Any]:
    """Change the named fields of a prompt."""
    rows = db.query(
        sql.PATCH_PROMPT,
        [
            prompt_id,
            read_body_string(body, "title"),
            read_body_string(body, "content"),
            read_body_string(body, "status"),
        ],
    )
    if not rows:
        raise not_found(f'No prompt "{prompt_id}"')
    return rows[0]


def drop(prompt_id: str) -> dict[str, Any]:
    """Delete a prompt; its plan stays."""
    rows = db.query(sql.DROP_PROMPT, [prompt_id])
    if not rows:
        raise not_found(f'No prompt "{prompt_id}". Nothing was deleted.')
    return rows[0]


@router.get("/prompts")
def _prompts(request: Request) -> Response:
    return reply(
        prompts(
            read_query(request, "project"),
            read_query(request, "status"),
            read_query(request, "plan"),
            read_query(request, "q"),
            read_limit(request),
            read_offset(request),
        )
    )


@router.get("/prompts/facets")
def _facets() -> Response:
    return reply(facets())


@router.get("/prompt")
def _prompt(request: Request) -> Response:
    return reply(prompt(require_query(request, "id")))


@router.post("/prompts")
def _save(body: Body) -> Response:
    saved = save(body)
    return reply(saved, 201 if saved["created"] else 200)


@router.patch("/prompt")
def _patch(request: Request, body: Body) -> Response:
    return reply(patch(require_query(request, "id"), body))


@router.delete("/prompt")
def _drop(request: Request) -> Response:
    return reply(drop(require_query(request, "id")))
