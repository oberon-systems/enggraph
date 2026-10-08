"""The tool list: 41 tools, their descriptions and the arguments they take.

The list is data in `tools.json`. Six descriptions depend on the project a
session was opened on and are written here.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

MEMORY_PROJECT = "_memory"
SUGGESTIONS_PROJECT = "_suggestions"
PLANS_PROJECT = "_plans"
# A record about no repository in particular needs a segment of its own in
# its node id, and "*" is not one.
GLOBAL_SCOPE = "_global"


def project_description(session_project: str | None) -> str:
    """Describe the `project` argument of a graph read."""
    if session_project is None:
        return (
            "Project to query. Required here: this session was opened on /mcp "
            "without naming one. list_projects returns the available names, and "
            "the tree each one reads."
        )
    return (
        f'Project to query. Defaults to "{session_project}"; name another '
        "indexed project to read its graph instead."
    )


def plan_scope_description(session_project: str | None, writing: bool) -> str:
    """Describe the `project` argument of a plan tool."""
    star = (
        '"*" saves it as a global plan, listed under every project.'
        if writing
        else '"*" lists the plans of every project.'
    )
    subject = "Project this plan is about" if writing else "Project to list"
    if session_project is None:
        return (
            f"{subject}. Required here: this session was opened on /mcp without "
            f"naming one. {star}"
        )
    return f'{subject}. Defaults to "{session_project}". {star}'


def search_scope_description(session_project: str | None) -> str:
    """Describe the `project` argument of a search."""
    base = (
        "Project to search. This session was opened on /mcp without naming "
        "one, so a search here spans every project unless one is named."
        if session_project is None
        else f'Project to search. Defaults to "{session_project}".'
    )
    return base + ' "*" searches every project; pass project_type to narrow that.'


def record_scope_description(session_project: str | None, noun: str) -> str:
    """Describe what a memory or a suggestion is about."""
    base = (
        f"What this {noun} is about. Required when writing: this session was "
        "opened on /mcp without naming a project."
        if session_project is None
        else f'What this {noun} is about. Defaults to "{session_project}".'
    )
    return (
        base + f' "*" is a {noun} about no project in particular, and those are read '
        "alongside every scope."
    )


def _held(members: int) -> str:
    if members == 0:
        return "holds no projects yet"
    return f"holds {members} project{'' if members == 1 else 's'}"


def organization_note(project: str, members: int) -> str:
    """Tell a session opened on an organization what a read there covers."""
    return (
        f' "{project}" is an organization: it {_held(members)} and reads no '
        "directory of its own, so a read naming it covers every project it holds "
        "and says which one answered. describe_project lists them. Name a member "
        "to read just that one, and to write anything into a graph."
    )


def organization_record_note(project: str, members: int) -> str:
    """Tell a session opened on an organization whose records it reads."""
    return (
        f' "{project}" is an organization: it {_held(members)}, a read here '
        "covers its own records and every member's, and a record written about it "
        "belongs to the organization rather than to any one member."
    )


@cache
def template() -> list[dict[str, Any]]:
    """Return the tool list as written, placeholders in place."""
    path = Path(__file__).with_name("tools.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _filled(value: Any, pieces: dict[str, str]) -> Any:  # noqa: ANN401
    if isinstance(value, str):
        for name, text in pieces.items():
            value = value.replace("{{" + name + "}}", text)
        return value
    if isinstance(value, list):
        return [_filled(item, pieces) for item in value]
    if isinstance(value, dict):
        return {key: _filled(item, pieces) for key, item in value.items()}
    return value


def list_tools(
    session_project: str | None, members: int | None
) -> list[dict[str, Any]]:
    """Return the tools as a session on this project is shown them.

    `members` is how many projects the session's organization holds, or None
    when it is no organization or could not be looked up.
    """
    note = record_note = ""
    if session_project is not None and members is not None:
        note = organization_note(session_project, members)
        record_note = organization_record_note(session_project, members)
    pieces = {
        "project": project_description(session_project) + note,
        "planTag": plan_scope_description(session_project, True) + record_note,
        "planFilter": plan_scope_description(session_project, False) + record_note,
        "memoryScope": record_scope_description(session_project, "memory")
        + record_note,
        "suggestionScope": record_scope_description(session_project, "suggestion")
        + record_note,
        "searchScope": search_scope_description(session_project),
    }
    return _filled(template(), pieces)
