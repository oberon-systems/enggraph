"""The code a record is about, and the records about a piece of code."""

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
    read_query,
    reply,
    require_body_string,
    require_query,
)
from enggraph.web.routes.projects import require_project

RECORD_PROJECTS = {
    "memory": "_memory",
    "plan": "_plans",
    "suggestion": "_suggestions",
}
GROUPS = ("kind", "lever", "about", "directory")
ROOT_ID = "./"

router = APIRouter()


def record_project(kind: str) -> str:
    """Return the built-in project a kind of record lives in."""
    project = RECORD_PROJECTS.get(kind)
    if project is None:
        raise bad_request(f"A record is one of {', '.join(RECORD_PROJECTS)}")
    return project


def around_node(node_id: str) -> list[str]:
    """Return the node and every directory above it, up to the root."""
    path = node_id.split("::")[0].rstrip("/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    ids = [node_id]
    for end in range(len(parts) - 1, 0, -1):
        ids.append("/".join(parts[:end]) + "/")
    if node_id != ROOT_ID:
        ids.append(ROOT_ID)
    return list(dict.fromkeys(ids))


def record_nodes(kind: str, record_id: str) -> list[dict[str, Any]]:
    """List the code a record is about."""
    return db.query(sql.RECORD_NODES, [record_project(kind), record_id])


def add_record_node(kind: str, record_id: str, body: object) -> dict[str, Any]:
    """Tie a record to a node."""
    record = record_project(kind)
    project = require_project(require_body_string(body, "project"))
    node_id = require_body_string(body, "node_id")
    added = db.query(sql.ADD_RECORD_NODE, [record, record_id, project, node_id])
    if not added:
        linked = db.query(sql.RECORD_NODES, [record, record_id])
        already = any(
            row["project"] == project and row["node_id"] == node_id for row in linked
        )
        if not already:
            raise not_found(
                f'No node "{node_id}" in {project}, or no record {record_id}'
            )
    return {"project": project, "node_id": node_id}


def drop_record_node(
    kind: str, record_id: str, project: str, node_id: str
) -> dict[str, Any]:
    """Untie a record from a node."""
    rows = db.query(
        sql.DROP_RECORD_NODE, [record_project(kind), record_id, project, node_id]
    )
    if not rows:
        raise not_found("That node is not linked to this record")
    return rows[0]


def knowledge(name: str, node_id: str) -> list[dict[str, Any]]:
    """List what was written about a node or a directory above it."""
    return db.query(sql.NODE_KNOWLEDGE, [name, around_node(node_id)])


def groups(by: str, status: str | None) -> dict[str, Any]:
    """Roll the suggestions up by one of their fields, or by directory."""
    if by not in GROUPS:
        raise bad_request(f"Group by one of {', '.join(GROUPS)}")
    if by == "directory":
        rows = db.query(sql.SUGGESTION_DIRECTORIES, [status])
    else:
        rows = db.query(sql.SUGGESTION_GROUPS, [by, status])
    return {"by": by, "groups": rows}


@router.get("/records/{kind}/nodes")
def _record_nodes(kind: str, request: Request) -> Response:
    project = record_project(kind)
    return reply(db.query(sql.RECORD_NODES, [project, require_query(request, "id")]))


@router.post("/records/{kind}/nodes")
def _add_record_node(kind: str, request: Request, body: Body) -> Response:
    record_project(kind)
    return reply(add_record_node(kind, require_query(request, "id"), body))


@router.delete("/records/{kind}/nodes")
def _drop_record_node(kind: str, request: Request) -> Response:
    record_project(kind)
    return reply(
        drop_record_node(
            kind,
            require_query(request, "id"),
            require_query(request, "project"),
            require_query(request, "node_id"),
        )
    )


@router.get("/projects/{name}/knowledge")
def _knowledge(name: str, request: Request) -> Response:
    name = require_project(name)
    return reply(knowledge(name, require_query(request, "id")))


@router.get("/suggestions/groups")
def _groups(request: Request) -> Response:
    by = read_query(request, "by") or "kind"
    return reply(groups(by, read_query(request, "status")))
