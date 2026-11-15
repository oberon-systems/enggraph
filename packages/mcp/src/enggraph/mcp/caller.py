"""Who a tool call comes from, and which projects that caller may read."""

from __future__ import annotations

from contextvars import ContextVar

from enggraph.core import authn, db

ORGANIZATION_TYPE = "organization"

CALLER: ContextVar[authn.Principal] = ContextVar(
    "enggraph_caller", default=authn.ANONYMOUS
)


def readable(projects: tuple[str, ...]) -> set[str]:
    """Return the projects a token names, and the members of its organizations."""
    rows = db.query(
        """SELECT m.project
       FROM org_members AS m
       JOIN projects AS p ON p.name = m.organization
      WHERE m.organization = ANY($1::text[]) AND p.type = $2""",
        [list(projects), ORGANIZATION_TYPE],
    )
    return {*projects, *(row["project"] for row in rows)}


def held_projects() -> set[str] | None:
    """Return the projects the caller may read, or None when it is every one."""
    who = CALLER.get()
    if who.kind == "anonymous" or authn.ALL_PROJECTS in who.projects:
        return None
    return readable(who.projects)


def held_list() -> list[str] | None:
    """Return the same as a sorted list, which is what a statement is given."""
    held = held_projects()
    return None if held is None else sorted(held)
