"""Roadmaps: ordered items, each tied to the plan that carries it out."""

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
    read_body_number,
    read_body_string,
    read_query,
    reply,
    require_body_string,
    require_query,
)
from enggraph.web.routes.plans import tag

MAX_POSITION = 1_000_000

router = APIRouter()


def roadmaps(scope: str | None, status: str | None) -> list[dict[str, Any]]:
    """Return the roadmaps of a scope and the global ones, items in order."""
    rows = db.query(sql.ROADMAPS, [tag(scope), status])
    return with_items(rows)


def with_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add `items` to every roadmap."""
    if not rows:
        return []
    held: dict[str, list[dict[str, Any]]] = {}
    for item in db.query(sql.ROADMAP_ITEMS, [[row["id"] for row in rows]]):
        held.setdefault(item["roadmap_id"], []).append(item)
    return [{**row, "items": held.get(row["id"], [])} for row in rows]


def roadmap(roadmap_id: str) -> dict[str, Any]:
    """Return one roadmap with its items."""
    rows = db.query(sql.ROADMAP, [roadmap_id])
    if not rows:
        raise not_found(f'No roadmap "{roadmap_id}"')
    return with_items(rows)[0]


def of_plan(plan_id: str) -> list[dict[str, Any]]:
    """Return the roadmap items one plan carries out."""
    return db.query(sql.PLAN_ITEMS, [plan_id])


def save(body: object) -> dict[str, Any]:
    """Write a roadmap, new or over the one of the same id; items stay."""
    roadmap_id = require_body_string(body, "id")
    status = read_body_string(body, "status")
    rows = db.query(
        sql.SAVE_ROADMAP,
        [
            roadmap_id,
            tag(read_body_string(body, "project")),
            require_body_string(body, "title"),
            read_body_string(body, "content"),
            "active" if status is None else status,
        ],
    )
    return {"id": roadmap_id, "created": rows[0]["created"]}


def patch(roadmap_id: str, body: object) -> dict[str, Any]:
    """Change the named fields of a roadmap."""
    rows = db.query(
        sql.PATCH_ROADMAP,
        [
            roadmap_id,
            read_body_string(body, "title"),
            read_body_string(body, "content"),
            read_body_string(body, "status"),
        ],
    )
    if not rows:
        raise not_found(f'No roadmap "{roadmap_id}"')
    return rows[0]


def drop(roadmap_id: str) -> dict[str, Any]:
    """Delete a roadmap and, after it, its items."""
    with db.transaction() as client:
        rows = client.query(sql.DROP_ROADMAP, [roadmap_id])
        # In a statement of its own, after the roadmap: see the `database` skill.
        items = client.query(sql.DROP_ROADMAP_ITEMS, [roadmap_id]) if rows else []
    if not rows:
        raise not_found(f'No roadmap "{roadmap_id}". Nothing was deleted.')
    return {**rows[0], "items": len(items)}


def _fields(body: object) -> list[Any]:
    """Read what an item can carry, each None when the body leaves it out."""
    return [
        read_body_number(body, "position", 1, MAX_POSITION),
        read_body_string(body, "section"),
        read_body_string(body, "title"),
        read_body_string(body, "content"),
        read_body_string(body, "status"),
        read_body_string(body, "plan_id"),
    ]


def _write_item(
    roadmap_id: str, item_id: str, body: object, new: bool
) -> list[dict[str, Any]]:
    """Write one item under its roadmap and its plan, both checked first."""
    position, section, title, content, status, plan_id = _fields(body)
    if new and (title is None or title.strip() == ""):
        raise bad_request('Field "title" is required')
    with db.transaction() as client:
        if not client.query(sql.LOCK_ROADMAP, [roadmap_id]):
            raise not_found(f'No roadmap "{roadmap_id}"')
        if plan_id and not client.query(sql.LOCK_PLAN, [plan_id]):
            raise not_found(f'No plan "{plan_id}"')
        if position is not None:
            client.query(sql.SHIFT_ROADMAP_ITEMS, [roadmap_id, item_id, position])
        return client.query(
            sql.ADD_ROADMAP_ITEM if new else sql.PATCH_ROADMAP_ITEM,
            [roadmap_id, item_id, position, section, title, content, status, plan_id],
        )


def add_item(roadmap_id: str, body: object) -> dict[str, Any]:
    """Add an item to a roadmap, last unless a position is named."""
    item_id = require_body_string(body, "id")
    if not _write_item(roadmap_id, item_id, body, new=True):
        raise bad_request(f'Roadmap "{roadmap_id}" already has an item "{item_id}"')
    return {"roadmap_id": roadmap_id, "id": item_id}


def patch_item(roadmap_id: str, item_id: str, body: object) -> dict[str, Any]:
    """Change the named fields of an item, or move it."""
    rows = _write_item(roadmap_id, item_id, body, new=False)
    if not rows:
        raise not_found(f'No item "{item_id}" in roadmap "{roadmap_id}"')
    return rows[0]


def drop_item(roadmap_id: str, item_id: str) -> dict[str, Any]:
    """Delete one item of a roadmap."""
    rows = db.query(sql.DROP_ROADMAP_ITEM, [roadmap_id, item_id])
    if not rows:
        raise not_found(
            f'No item "{item_id}" in roadmap "{roadmap_id}". Nothing was deleted.'
        )
    return rows[0]


@router.get("/roadmaps")
def _roadmaps(request: Request) -> Response:
    return reply(
        roadmaps(read_query(request, "project"), read_query(request, "status"))
    )


@router.get("/roadmap")
def _roadmap(request: Request) -> Response:
    return reply(roadmap(require_query(request, "id")))


@router.post("/roadmaps")
def _save(body: Body) -> Response:
    saved = save(body)
    return reply(saved, 201 if saved["created"] else 200)


@router.patch("/roadmap")
def _patch(request: Request, body: Body) -> Response:
    return reply(patch(require_query(request, "id"), body))


@router.delete("/roadmap")
def _drop(request: Request) -> Response:
    return reply(drop(require_query(request, "id")))


@router.post("/roadmap/items")
def _add_item(request: Request, body: Body) -> Response:
    return reply(add_item(require_query(request, "id"), body), 201)


@router.patch("/roadmap/item")
def _patch_item(request: Request, body: Body) -> Response:
    return reply(
        patch_item(require_query(request, "id"), require_query(request, "item"), body)
    )


@router.delete("/roadmap/item")
def _drop_item(request: Request) -> Response:
    return reply(
        drop_item(require_query(request, "id"), require_query(request, "item"))
    )
