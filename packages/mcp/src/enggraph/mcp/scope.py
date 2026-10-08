"""Read a call's arguments, and settle which projects it is about."""

from __future__ import annotations

import math
from typing import Any

from enggraph.core import db, jsjson
from enggraph.mcp.context import DEFAULT_EXPAND
from enggraph.mcp.errors import ToolError
from enggraph.mcp.knowledge import records_about
from enggraph.mcp.tools import GLOBAL_SCOPE, MEMORY_PROJECT, SUGGESTIONS_PROJECT

MAX_RESULTS = 50
DEFAULT_TEXT_LINES = 50
MAX_TEXT_LINES = 500
DEFAULT_RESULTS = 20
# A path search walks the edge table once per hop, so the ceiling is what
# keeps a question about two unrelated nodes from scanning the whole graph.
MAX_HOPS = 10
DEFAULT_HOPS = 6
MAX_EXPAND_HOPS = 2
# A project that is no tree of its own but a set of them.
ORGANIZATION_TYPE = "organization"

Args = dict[str, Any] | None
Result = dict[str, Any]


def given(args: Args, key: str) -> Any:  # noqa: ANN401
    """Return an argument, or None when the call did not carry it."""
    return None if args is None else args.get(key)


def is_number(value: Any) -> bool:  # noqa: ANN401
    """Say whether a value is a finite number, as a JSON number is."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)


def require_string(args: Args, key: str) -> str:
    """Return a string argument that must be there."""
    value = given(args, key)
    if not isinstance(value, str) or value.strip() == "":
        raise ToolError(f'Argument "{key}" is required and must be a non-empty string')
    return value


def read_bounded(args: Args, key: str, fallback: int, low: int, high: int) -> int:
    """Read a bounded integer argument, falling back to its default."""
    value = given(args, key)
    if not is_number(value):
        return fallback
    return min(max(math.trunc(value), low), high)


def read_hops(args: Args) -> int:
    """Clamp an optional hop budget into [1, MAX_HOPS]."""
    return read_bounded(args, "max_hops", DEFAULT_HOPS, 1, MAX_HOPS)


def read_limit(args: Args) -> int:
    """Clamp an optional numeric limit into [1, MAX_RESULTS]."""
    return read_bounded(args, "limit", DEFAULT_RESULTS, 1, MAX_RESULTS)


def read_detail(args: Args) -> str:
    """Read how much of the source a packet should carry."""
    value = given(args, "detail")
    if value is None and (args is None or "detail" not in args):
        return "auto"
    if value not in ("auto", "summary", "source") or not isinstance(value, str):
        raise ToolError('Argument "detail" must be "auto", "summary" or "source"')
    return value


def read_expand(args: Args) -> dict[str, int]:
    """Read the per-tier hop counts get_context expands by."""
    asked = given(args, "expand")
    if not isinstance(asked, dict | list):
        return dict(DEFAULT_EXPAND)
    source = asked if isinstance(asked, dict) else {}

    def hops(key: str, fallback: int) -> int:
        return read_bounded(source, key, fallback, 0, MAX_EXPAND_HOPS)

    return {
        "caller": hops("callers", DEFAULT_EXPAND["caller"]),
        "callee": hops("callees", DEFAULT_EXPAND["callee"]),
        "test": hops("tests", DEFAULT_EXPAND["test"]),
        "import": hops("imports", DEFAULT_EXPAND["import"]),
        "defines": hops("defines", DEFAULT_EXPAND["defines"]),
    }


def read_project(args: Args, session_project: str | None) -> str:
    """Pick the project a call is about: its own argument, else the session's."""
    value = given(args, "project")
    if isinstance(value, str) and value.strip() != "":
        return value.strip()
    if session_project is not None:
        return session_project
    raise ToolError(
        'Argument "project" is required: this session was opened on /mcp without '
        "naming one. Connect to /mcp/<project> instead, or pass the argument. "
        "list_projects returns the available names."
    )


def read_optional_string(args: Args, key: str) -> str | None:
    """Read an optional string argument, rejecting a present but wrong-typed one."""
    value = given(args, key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f'Argument "{key}" must be a string')
    return value.strip() or None


def read_tags(args: Args, key: str) -> list[str] | None:
    """Read an optional array-of-strings argument."""
    value = given(args, key)
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
        raise ToolError(f'Argument "{key}" must be an array of strings')
    tags = [tag.strip() for tag in value if tag.strip()]
    return tags or None


def read_search_project(args: Args, session_project: str | None) -> str | None:
    """Return which project a search covers, or None for every project.

    Unlike read_project this never refuses a session without a default: a
    search that names nothing spans the database.
    """
    named = read_optional_string(args, "project")
    if named is not None:
        return None if named == "*" else named
    if read_optional_string(args, "project_type") is not None:
        return None
    return session_project


def read_search_scope(
    args: Args, session_project: str | None
) -> tuple[str | None, str | None]:
    """Return the project and the type a database-wide search is narrowed by."""
    named = read_search_project(args, session_project)
    kind = read_optional_string(args, "project_type")
    if named is not None and kind is not None:
        raise ToolError(
            'Arguments "project" and "project_type" cannot be combined: '
            "project_type narrows a search across projects, so pass "
            'project: "*" or leave it out.'
        )
    if named is not None:
        require_project(named)
    return named, kind


def read_record_scope(
    args: Args, session_project: str | None
) -> tuple[bool, str | None]:
    """Return whether a record call named its scope, and the scope.

    `explicit` separates a caller that named "*" from one that named nothing
    on a session without a default: both are None and mean opposite things.
    """
    named = read_optional_string(args, "about")
    if named is not None:
        return True, None if named == "*" else named
    return False, session_project


def read_node_filter(args: Args, fallback: str | None) -> dict[str, str] | None:
    """Return the node a record read is narrowed to, in the project it is about."""
    node_id = read_optional_string(args, "node_id")
    if node_id is None:
        return None
    project = read_optional_string(args, "node_project") or fallback
    if project is None:
        raise ToolError(
            'Argument "node_project" is required: this read names no project the '
            "node could belong to"
        )
    return {"project": project, "node_id": node_id}


def attached_to(record_project: str, node: dict[str, str] | None) -> list[str] | None:
    """Return the ids of one kind of record attached to the node filter, if any."""
    return None if node is None else records_about(record_project, node)


def scoped_record_id(about: str | None, record_id: str) -> str:
    """Return the node id a record is stored under: its scope, then its slug."""
    if "/" in record_id:
        return record_id
    return f"{about if about is not None else GLOBAL_SCOPE}/{record_id}"


def read_plan_scope(args: Args, session_project: str | None) -> tuple[bool, str | None]:
    """Return whether a plan call named its project, and the project."""
    value = given(args, "project")
    if isinstance(value, str) and value.strip() != "":
        tag = value.strip()
        return True, None if tag == "*" else tag
    return False, session_project


def unknown_project(project: str) -> ToolError:
    """Return the error an unknown name gets, naming what is registered instead."""
    rows = db.query("SELECT name FROM projects ORDER BY name")
    names = ", ".join(row["name"] for row in rows)
    return ToolError(
        f'No project named "{project}". Registered: {names or "none"}. '
        "Onboard one with `make install`, then index it from the dashboard."
    )


def require_project(project: str) -> str:
    """Reject an unknown project by name rather than by empty result."""
    if not db.query("SELECT 1 FROM projects WHERE name = $1", [project]):
        raise unknown_project(project)
    return project


def read_scope(project: str) -> dict[str, Any]:
    """Return what a read covers: one project, or what an organization holds.

    An organization reads no directory of its own, so its row carries no
    graph: reading it as its members is what the type is for.
    """
    rows = db.query(
        """SELECT p.type,
            (SELECT array_agg(m.project ORDER BY m.created_at, m.project)
               FROM org_members AS m
              WHERE m.organization = p.name) AS members
       FROM projects AS p
      WHERE p.name = $1""",
        [project],
    )
    if not rows:
        raise unknown_project(project)
    row = rows[0]
    if row["type"] != ORGANIZATION_TYPE:
        return {"project": project, "organization": False, "members": [project]}
    return {"project": project, "organization": True, "members": row["members"] or []}


def text_result(text: str, is_error: bool = False) -> Result:
    """Wrap one text into a tool answer."""
    result: Result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def empty_organization(scope: dict[str, Any]) -> Result:
    """Say that the organization a read names holds nothing, rather than nothing."""
    return text_result(
        f'"{scope["project"]}" is an organization and holds no projects yet, '
        "so there is nothing under it to read. Add projects to it from the "
        "dashboard, and every read here covers all of them."
    )


def write_needs_member(scope: dict[str, Any], what: str) -> Result:
    """Refuse a write aimed at an organization, naming what it holds."""
    held = (
        f"holds {', '.join(scope['members'])}"
        if scope["members"]
        else "holds no projects yet"
    )
    return text_result(
        f'"{scope["project"]}" is an organization: it {held} and reads no '
        f"directory of its own, so there is no graph in it to {what}. "
        "Name the member to write to with the project argument.",
        True,
    )


# Both written out whole, so the check that prepares every statement against
# the schema reads them.
PROJECT_PROFILE = """
  SELECT p.name, p.type, p.description, p.indexed_at, p.root_path,
         (SELECT count(*)::int FROM nodes AS g
           WHERE g.project = p.name) AS nodes
    FROM projects AS p WHERE p.name = $1"""

MEMBER_PROFILES = """
  SELECT p.name, p.type, p.description, p.indexed_at, p.root_path,
         (SELECT count(*)::int FROM nodes AS g
           WHERE g.project = p.name) AS nodes, m.owned
       FROM org_members AS m
       JOIN projects AS p ON p.name = m.project
      WHERE m.organization = $1
      ORDER BY m.created_at, m.project"""


def describe_project(project: str) -> db.Row:
    """Describe one project: what it is, not what is in its graph."""
    rows = db.query(PROJECT_PROFILE, [project])
    if not rows:
        raise unknown_project(project)
    return rows[0]


def describe_members(organization: str) -> list[db.Row]:
    """Describe the projects an organization holds, in the order they joined."""
    return db.query(MEMBER_PROFILES, [organization])


def describe_holders(project: str) -> list[db.Row]:
    """Describe the organizations holding a project, in the order it joined them."""
    return db.query(
        """SELECT m.organization AS name, p.description, m.owned
       FROM org_members AS m
       JOIN projects AS p ON p.name = m.organization
      WHERE m.project = $1
      ORDER BY m.created_at, m.organization""",
        [project],
    )


def expand_record_scope(about: str | None) -> list[str] | None:
    """Return the scopes a record read covers, or None for every scope.

    A record's tag may name a codebase never indexed, so an unknown name
    answers with itself; an organization answers with itself and its members.
    """
    if about is None:
        return None
    rows = db.query(
        """SELECT (SELECT array_agg(m.project ORDER BY m.created_at, m.project)
               FROM org_members AS m
              WHERE m.organization = p.name) AS members
       FROM projects AS p
      WHERE p.name = $1 AND p.type = $2""",
        [about, ORGANIZATION_TYPE],
    )
    if not rows:
        return [about]
    return [about, *(rows[0]["members"] or [])]


# Counted per project rather than summed: one index run brings the derived
# rows back, and nothing brings a hand-written summary back.
DROP_REPORT = """
  SELECT p.root_path, p.indexed_at, p.type,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = p.name) AS nodes,
         (SELECT count(*) FROM nodes AS g
           WHERE g.type = 'memory'
             AND (g.project = p.name
                  OR g.metadata ->> 'about' = p.name)) AS memories,
         (SELECT count(*) FROM nodes AS g
           WHERE g.type = 'suggestion'
             AND (g.project = p.name
                  OR g.metadata ->> 'about' = p.name)) AS suggestions,
         (SELECT count(*) FROM edges AS e
           WHERE e.project = p.name) AS edges,
         (SELECT count(*) FROM indexed_files AS f
           WHERE f.project = p.name) AS hashes,
         (SELECT count(*) FROM chunks AS c
           WHERE c.project = p.name) AS embeddings,
         (SELECT count(*) FROM nodes AS l
           WHERE l.project = '_plans'
             AND l.metadata ->> 'about' = p.name) AS plans,
         (SELECT count(*) FROM nodes AS g
           WHERE g.project = p.name
             AND g.type <> 'memory'
             AND g.metadata ->> 'summary_source' = 'manual') AS summaries,
         (SELECT count(*) FROM declared_links AS r
           WHERE r.source_project = p.name
              OR r.target_project = p.name) AS relations,
         (SELECT count(*) FROM provided_names AS x
           WHERE x.project = p.name AND x.origin = 'manual') AS exports,
         (SELECT count(*) FROM record_links AS k
           WHERE k.project = p.name) AS record_links
    FROM projects AS p
   WHERE p.name = $1"""


def describe_drop(project: str, row: db.Row, dropped: bool) -> str:
    """Render a drop, before or after it happened."""
    when = (
        "never indexed"
        if row["indexed_at"] is None
        else f"indexed {jsjson.instant(row['indexed_at'])}"
    )
    # Nothing rebuilds a built-in project: there is no tree behind one.
    builtin = row["type"] in ("memory", "suggestions")
    derived = (
        []
        if builtin
        else [
            f"  rebuilt by one index run: {row['nodes']} nodes, "
            f"{row['edges']} edges, {row['hashes']} file hashes, "
            f"{row['embeddings']} embeddings"
        ]
    )
    lost = [f"{row['summaries']} manual summaries"]
    if row["record_links"] != "0":
        lost.append(
            f"{row['record_links']} links from memories, plans and suggestions to "
            "its nodes; the records themselves are kept"
        )
    if row["relations"] != "0" or row["exports"] != "0":
        lost.append(
            f"{row['relations']} relations declared with other projects and "
            f"{row['exports']} exports declared by hand"
        )
    # A record about this project lives under a built-in project, so it is
    # kept and goes on naming a project that is gone.
    if row["memories"] != "0" and builtin:
        lost.append(f"{row['memories']} memories, which no index run brings back")
    kept = [f'{row["plans"]} plans, still readable with get_plans project: "{project}"']
    if row["memories"] != "0" and not builtin:
        kept.append(f"{row['memories']} memories about it, in {MEMORY_PROJECT}")
    if row["suggestions"] != "0":
        kept.append(
            f"{row['suggestions']} suggestions about it, in {SUGGESTIONS_PROJECT}"
        )
    head = "dropped" if dropped else "project"
    lines = [
        f'{head} "{project}" ({row["root_path"]}, {when})',
        *derived,
        f"  not rebuilt, gone for good: {', '.join(lost)}",
        f"  kept, tagged with the name: {', '.join(kept)}",
    ]
    if not dropped:
        lines.append("Nothing was deleted. Call again with confirm: true to drop it.")
    return "\n".join(lines)
