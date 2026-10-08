"""Skills: the ones the server hands out, and which project gets which."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.core import db
from enggraph.web import queries as sql
from enggraph.web.args import (
    Body,
    bad_request,
    count,
    field,
    not_found,
    number,
    read_body_string,
    read_query,
    reply,
    require_body_string,
    require_query,
    whole,
)
from enggraph.web.routes.plans import GLOBAL_ONLY, scoped
from enggraph.web.routes.projects import require_project

SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONTMATTER = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---")
NAME_LINE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)

router = APIRouter()


def frontmatter_name(content: str) -> str | None:
    """Return the `name:` of a SKILL.md frontmatter, what the skill is called."""
    block = FRONTMATTER.match(content)
    name = None if block is None else NAME_LINE.search(block.group(1))
    return None if name is None else name.group(1)


def read_id(value: str) -> int:
    """Read a skill id, or refuse what is not one."""
    parsed = number(value)
    if not whole(parsed) or parsed <= 0:
        raise bad_request(f'"{value}" is not a skill id')
    return int(parsed)


def skills(scope: str | None) -> list[dict[str, Any]]:
    """List the skills of a scope."""
    named, global_only = scoped(scope)
    rows = db.query(sql.SKILLS, [named, global_only])
    return [{**row, "length": count(row["length"])} for row in rows]


def skill(skill_id: int) -> dict[str, Any]:
    """Return one skill with its text."""
    rows = db.query(sql.SKILL, [skill_id])
    if not rows:
        raise not_found(f"No skill {skill_id}")
    return rows[0]


def import_skill(body: object) -> dict[str, Any]:
    """Store a skill from its text, for every project or for one."""
    content = require_body_string(body, "content")
    owner = read_body_string(body, "owner")
    project = None if owner in (None, "", GLOBAL_ONLY) else require_project(owner)
    name = frontmatter_name(content)
    if name is None or SKILL_NAME.match(name) is None:
        raise bad_request(
            "The text needs a frontmatter `name:` of lowercase letters, digits "
            "and dashes"
        )
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    rows = db.query(sql.IMPORT_SKILL, [project, name, content, sha256])
    if not rows:
        raise bad_request(f'"{name}" is a built-in skill and cannot be replaced')
    return rows[0]


def drop(skill_id: int) -> dict[str, Any]:
    """Delete an imported skill and its switches."""
    rows = db.query(sql.DROP_SKILL, [skill_id])
    if not rows:
        raise not_found(f"No imported skill {skill_id}. Built-in skills stay.")
    return rows[0]


def project_skills(name: str) -> list[dict[str, Any]]:
    """List every skill a project could have, and whether it does."""
    return db.query(sql.PROJECT_SKILLS, [name])


def switch(name: str, skill_id: str, body: object) -> dict[str, Any]:
    """Switch a skill on or off for a project."""
    enabled = field(body, "enabled")
    if not isinstance(enabled, bool):
        raise bad_request('Field "enabled" must be a boolean')
    rows = db.query(sql.SET_SKILL_ENABLED, [name, read_id(skill_id), enabled])
    if not rows:
        raise bad_request("That skill cannot be switched here")
    return rows[0]


@router.get("/skills")
def _skills(request: Request) -> Response:
    return reply(skills(read_query(request, "owner")))


@router.get("/skill")
def _skill(request: Request) -> Response:
    return reply(skill(read_id(require_query(request, "id"))))


@router.post("/skills")
def _import(body: Body) -> Response:
    return reply(import_skill(body))


@router.delete("/skill")
def _drop(request: Request) -> Response:
    return reply(drop(read_id(require_query(request, "id"))))


@router.get("/projects/{name}/skills")
def _project_skills(name: str) -> Response:
    return reply(project_skills(require_project(name)))


@router.put("/projects/{name}/skills/{skill_id}")
def _switch(name: str, skill_id: str, body: Body) -> Response:
    return reply(switch(require_project(name), skill_id, body))
