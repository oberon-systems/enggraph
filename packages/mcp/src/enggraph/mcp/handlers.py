"""What each tool does with a call: one function per tool, and the dispatch."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from enggraph.core import db, jsjson
from enggraph.mcp import collate
from enggraph.mcp.context import (
    DEFAULT_SEEDS,
    DEFAULT_TOKEN_BUDGET,
    MAX_SEEDS,
    MAX_TOKEN_BUDGET,
    MIN_TOKEN_BUDGET,
    build_context,
)
from enggraph.mcp.errors import ToolError, message
from enggraph.mcp.knowledge import (
    SUGGESTION_GROUPS,
    group_suggestions,
    knowledge_counts,
    knowledge_for,
    narrow_ids,
    parse_node_refs,
    replace_record_nodes,
    require_nodes,
    with_nodes,
)
from enggraph.mcp.links import (
    DEFAULT_LINK_DEPTH,
    LINK_DIRECTIONS,
    LINK_KINDS,
    MAX_LINK_DEPTH,
    drop_export,
    drop_relation,
    find_name,
    link_summary,
    node_exists,
    node_links,
    normalize_name,
    project_links,
    save_export,
    save_relation,
)
from enggraph.mcp.overview import (
    DEFAULT_OVERVIEW_BUDGET,
    DEFAULT_OVERVIEW_DEPTH,
    MAX_OVERVIEW_DEPTH,
    build_overview,
)
from enggraph.mcp.rerank import rerank
from enggraph.mcp.scope import (
    DEFAULT_TEXT_LINES,
    DROP_REPORT,
    MAX_RESULTS,
    MAX_TEXT_LINES,
    ORGANIZATION_TYPE,
    Args,
    Result,
    attached_to,
    describe_drop,
    describe_holders,
    describe_members,
    describe_project,
    empty_organization,
    expand_record_scope,
    given,
    read_bounded,
    read_detail,
    read_expand,
    read_hops,
    read_limit,
    read_node_filter,
    read_optional_string,
    read_plan_scope,
    read_project,
    read_record_scope,
    read_scope,
    read_search_project,
    read_search_scope,
    read_tags,
    require_project,
    require_string,
    scoped_record_id,
    text_result,
    write_needs_member,
)
from enggraph.mcp.search import MODES, hybrid_search, mode_note, semantic_note
from enggraph.mcp.skills import effective_skills, skill_path, stamp
from enggraph.mcp.symbols import (
    DEFAULT_IMPACT_DEPTH,
    DEFAULT_SYMBOL_HOPS,
    MAX_SYMBOL_HOPS,
    find_definition,
    find_symbol,
    impact_analysis,
)
from enggraph.mcp.tools import MEMORY_PROJECT, PLANS_PROJECT, SUGGESTIONS_PROJECT
from enggraph.mcp.trace import DEFAULT_TRACE_STEPS, MAX_TRACE_STEPS, trace_node
from enggraph.mcp.worker import drop_project, grep_trees

LOG = logging.getLogger(__name__)


def present(args: Args, key: str) -> bool:
    """Say whether a call carried an argument at all, null included."""
    return args is not None and key in args


def json_result(value: Any) -> Result:  # noqa: ANN401
    """Wrap a value into a tool answer, printed the way it always was."""
    return text_result(jsjson.dumps(value, 2))


def read_typed(
    args: Args,
    key: str,
    kind: type,
    word: str,
    fallback: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Read an optional argument that must be of one JSON type when given."""
    if not present(args, key):
        return fallback
    value = given(args, key)
    if not isinstance(value, kind):
        raise ToolError(f'Argument "{key}" must be {word}')
    return value


def known_project(name: str) -> bool:
    """Say whether a project of this name is registered."""
    return bool(db.query("SELECT 1 FROM projects WHERE name = $1", [name]))


def list_projects(args: Args, session_project: str | None) -> Result:
    """List every project, with what holds it and what it holds."""
    return json_result(
        db.query(
            """SELECT p.name, p.type, p.description, p.root_path, p.indexed_at,
                  COUNT(n.id) AS nodes,
                  (SELECT coalesce(json_agg(m.project
                            ORDER BY m.created_at, m.project), '[]'::json)
                     FROM org_members AS m
                    WHERE m.organization = p.name) AS members,
                  (SELECT coalesce(json_agg(m.organization
                            ORDER BY m.created_at, m.organization), '[]'::json)
                     FROM org_members AS m
                    WHERE m.project = p.name) AS organizations
             FROM projects AS p
             LEFT JOIN nodes AS n ON n.project = p.name
            GROUP BY p.name, p.type, p.description, p.root_path, p.indexed_at
            ORDER BY p.name"""
        )
    )


def drop_project_tool(args: Args, session_project: str | None) -> Result:
    """Report what a drop costs, and drop only when confirmed."""
    # The project to drop is the one named in the call, never the session's.
    target = require_project(require_string(args, "name"))
    confirm = read_typed(args, "confirm", bool, "a boolean", False)
    report = db.query(DROP_REPORT, [target])[0]
    if confirm:
        # No table holds a key, so the worker API names every table the rows
        # live in and deletes them in one transaction.
        drop_project(target)
    return text_result(describe_drop(target, report, confirm))


def save_plan(args: Args, session_project: str | None) -> Result:
    """Write a plan, and the code it is about when that is named."""
    plan_id = require_string(args, "plan_id")
    title = require_string(args, "title")
    content = require_string(args, "content")
    status = read_typed(args, "status", str, "a string", "active")
    plan_type = read_typed(args, "type", str, "a string", "plan")

    explicit, project = read_plan_scope(args, session_project)
    # Storing a plan under no project at all is a decision, so it has to be
    # made rather than fallen into by omission.
    if not explicit and project is None:
        raise ToolError(
            'Argument "project" is required: this session was opened on /mcp '
            'without naming one. Name the project the plan is about, or "*" '
            "to save it as a global plan."
        )
    refs = parse_node_refs(given(args, "nodes"), project or session_project)
    if refs is not None:
        require_nodes(refs)

    with db.transaction() as client:
        # Re-created rather than assumed: a dropped built-in row would leave
        # every later save naming a project that is not there.
        client.query(
            """INSERT INTO projects (name, root_path, type)
             VALUES ($1, 'plans://agent', 'plans')
             ON CONFLICT (name) DO NOTHING""",
            [PLANS_PROJECT],
        )
        client.query(
            """INSERT INTO nodes (
               project, id, name, type, content, metadata
             )
             VALUES ($1, $2, $3, $4, $5,
                     JSONB_BUILD_OBJECT(
                       'about', $6::text,
                       'status', $7::text,
                       'summary_source', 'manual',
                       'updated_at', to_char(
                         now() AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                       )
                     ))
             ON CONFLICT (project, id) DO UPDATE SET
               name = EXCLUDED.name,
               type = EXCLUDED.type,
               content = EXCLUDED.content,
               metadata = nodes.metadata || EXCLUDED.metadata""",
            [PLANS_PROJECT, plan_id, title, plan_type, content, project, status],
        )
        if refs is not None:
            replace_record_nodes(client, PLANS_PROJECT, plan_id, refs)

    where = "global" if project is None else f"project {project}"
    text = [f"Plan {plan_id} successfully saved ({where})."]
    # The tag is free text with nothing to check it, so a typo would
    # otherwise store a plan under a name no session ever asks for.
    if project is not None and not known_project(project):
        text.append(
            f'No indexed project named "{project}". The plan was '
            "stored anyway and will be listed once one is indexed under "
            "that name."
        )
    return text_result("\n".join(text))


def get_plans(args: Args, session_project: str | None) -> Result:
    """List the plans of a scope, of one status and one type."""
    status = read_typed(args, "status", str, "a string", "active")
    # Defaulted like the status: an unfiltered listing would put a template
    # where an agent reads approved pending work.
    plan_type: str | None = read_typed(args, "type", str, "a string", "plan")
    if plan_type == "*":
        plan_type = None

    # A null scope is no filter at all; an organization widens to itself and
    # its members.
    _, project = read_plan_scope(args, session_project)
    about = expand_record_scope(project)
    attached = attached_to(
        PLANS_PROJECT, read_node_filter(args, project or session_project)
    )
    rows = db.query(
        """SELECT id,
                  metadata ->> 'about' AS project,
                  name AS title,
                  content,
                  metadata ->> 'status' AS status,
                  type,
                  metadata - 'about' - 'status' - 'summary_source'
                    - 'updated_at' AS metadata,
                  created_at,
                  metadata ->> 'updated_at' AS updated_at
             FROM nodes
            WHERE project = $1
              AND ($2::text[] IS NULL
                   OR metadata ->> 'about' = ANY ($2)
                   OR metadata ->> 'about' IS NULL)
              AND metadata ->> 'status' = $3
              AND ($4::text IS NULL OR type = $4)
              AND ($5::text[] IS NULL OR id = ANY ($5))
            ORDER BY (metadata ->> 'about' IS NULL),
                     metadata ->> 'about',
                     metadata ->> 'updated_at' DESC""",
        [PLANS_PROJECT, about, status, plan_type, attached],
    )
    return json_result(with_nodes(PLANS_PROJECT, rows))


def drop_plan(args: Args, session_project: str | None) -> Result:
    """Delete a plan and its links to code."""
    plan_id = require_string(args, "plan_id")
    rows = db.query(
        """WITH gone AS (
             DELETE FROM nodes
              WHERE project = $1 AND id = $2
          RETURNING project AS holder, id,
                    metadata ->> 'about' AS project,
                    name AS title,
                    metadata ->> 'status' AS status
           ), links AS (
             DELETE FROM record_links AS r USING gone
              WHERE r.record_project = gone.holder AND r.record_id = gone.id
           )
           SELECT project, title, status FROM gone""",
        [PLANS_PROJECT, plan_id],
    )
    # A typo and a delete have to read differently.
    if not rows:
        return text_result(f'No plan "{plan_id}". Nothing was deleted.')
    row = rows[0]
    where = "global" if row["project"] is None else f"project {row['project']}"
    return text_result(
        f'Deleted plan {plan_id} ({where}): "{row["title"]}", status {row["status"]}.'
    )


def unknown_scope_note(about: str | None, what: str, verb: str) -> list[str]:
    """Warn that a record was filed under a name no project has."""
    # The scope is free text with nothing to check it, so a typo would file
    # the record where nothing looks.
    if about is None or known_project(about):
        return []
    return [
        f'No project named "{about}" in this database. The {what} '
        f"was stored anyway and will be {verb} once one is indexed under "
        "that name."
    ]


def save_memory(args: Args, session_project: str | None) -> Result:
    """Write a memory, and the code it is about when that is named."""
    memory_id = require_string(args, "memory_id")
    title = require_string(args, "title")
    text = require_string(args, "text")
    gist = read_optional_string(args, "summary") or title
    tags = read_tags(args, "tags") or []
    explicit, about = read_record_scope(args, session_project)

    # Storing a memory against no project at all is a decision, as with a plan.
    if not explicit and about is None:
        raise ToolError(
            'Argument "about" is required: this session was opened on /mcp '
            'without naming a project. Name the project this is about, or "*" '
            "for a memory about none in particular."
        )
    if "/" in memory_id:
        raise ToolError(
            'Argument "memory_id" is a slug, not a path: the scope is taken '
            'from "about" and prefixed automatically.'
        )

    node_id = scoped_record_id(about, memory_id)
    refs = parse_node_refs(given(args, "nodes"), about or session_project)
    if refs is not None:
        require_nodes(refs)
    with db.transaction() as client:
        client.query(
            """INSERT INTO projects (name, root_path, type)
             VALUES ($1, 'memory://agent', 'memory')
             ON CONFLICT (name) DO NOTHING""",
            [MEMORY_PROJECT],
        )
        client.query(
            """INSERT INTO nodes (
               project, id, name, type, summary, content, metadata
             )
             VALUES ($1, $2, $3, 'memory', $4, $5,
                     JSONB_BUILD_OBJECT(
                       'about', $6::text,
                       'tags', $7::jsonb,
                       'summary_source', 'manual',
                       'updated_at', to_char(
                         now() AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                       )
                     ))
             ON CONFLICT (project, id) DO UPDATE SET
               name = EXCLUDED.name,
               type = 'memory',
               summary = EXCLUDED.summary,
               content = EXCLUDED.content,
               metadata = nodes.metadata || EXCLUDED.metadata""",
            [MEMORY_PROJECT, node_id, title, gist, text, about, jsjson.dumps(tags)],
        )
        if refs is not None:
            replace_record_nodes(client, MEMORY_PROJECT, node_id, refs)

    where = "no project in particular" if about is None else about
    lines = [f"Memory {node_id} saved (about {where})."]
    lines += unknown_scope_note(about, "memory", "read")
    return text_result("\n".join(lines))


def skill_tools(name: str, args: Args, session_project: str | None) -> Result:
    """List the skills a session should have, or hand one over."""
    project = None
    if session_project is not None or present(args, "project"):
        project = read_project(args, session_project)
    found = effective_skills(project)
    if name == "list_skills":
        return json_result(
            [
                {
                    "name": skill["name"],
                    "version": skill["sha256"],
                    "owner": "global" if skill["owner"] is None else skill["owner"],
                    "source": skill["source"],
                    "path": skill_path(skill["name"]),
                }
                for skill in found
            ]
        )
    wanted = require_string(args, "name")
    skill = next((row for row in found if row["name"] == wanted), None)
    if skill is None:
        raise ToolError(f'No skill "{wanted}" is enabled here; list_skills names them')
    return text_result(
        f"Write to {skill_path(skill['name'])}, version {skill['sha256']}:\n\n"
        + stamp(skill["content"], skill["sha256"])
    )


def record_ids(wanted: str | None, about: list[str] | None) -> list[str] | None:
    """Return the ids a slug may be stored under in the scopes a read sees."""
    if wanted is None:
        return None
    if "/" in wanted:
        return [wanted]
    # A bare slug is looked for in every scope this read can see and globally.
    return [
        *[scoped_record_id(one, wanted) for one in about or []],
        scoped_record_id(None, wanted),
    ]


def get_memory(args: Args, session_project: str | None) -> Result:
    """List the memories of a scope, narrowed by id, tags, words or a node."""
    _, scope = read_record_scope(args, session_project)
    wanted = read_optional_string(args, "memory_id")
    tags = read_tags(args, "tags")
    query = read_optional_string(args, "query")

    about = expand_record_scope(scope)
    attached = attached_to(
        MEMORY_PROJECT, read_node_filter(args, scope or session_project)
    )
    ids = narrow_ids(record_ids(wanted, about), attached)
    rows = db.query(
        """SELECT id, name AS title, summary, content,
                  metadata ->> 'about' AS about,
                  metadata -> 'tags' AS tags,
                  metadata ->> 'updated_at' AS updated_at,
                  created_at
             FROM nodes
            WHERE project = $1
              AND type = 'memory'
              AND ($2::text[] IS NULL OR id = ANY ($2))
              AND ($3::text[] IS NULL
                   OR metadata ->> 'about' = ANY ($3)
                   OR metadata ->> 'about' IS NULL)
              AND ($4::jsonb IS NULL OR metadata -> 'tags' @> $4)
              AND ($5::text IS NULL
                   OR name ILIKE $5 OR summary ILIKE $5 OR content ILIKE $5)
            ORDER BY (metadata ->> 'about' IS NULL), id
            LIMIT $6""",
        [
            MEMORY_PROJECT,
            ids,
            about,
            None if tags is None else jsjson.dumps(tags),
            None if query is None else f"%{query}%",
            read_limit(args),
        ],
    )
    return json_result(with_nodes(MEMORY_PROJECT, rows))


def drop_record(
    args: Args, session_project: str | None, key: str, noun: str, holder: str, sql: str
) -> Result:
    """Delete a memory or a suggestion and its links to code."""
    record_id = require_string(args, key)
    _, about = read_record_scope(args, session_project)
    node_id = scoped_record_id(about, record_id)
    rows = db.query(sql, [holder, node_id])
    # A typo and a delete have to read differently.
    if not rows:
        return text_result(f'No {noun} "{node_id}". Nothing was deleted.')
    return text_result(f'Dropped {noun} {node_id} ("{rows[0]["name"]}").')


def drop_memory(args: Args, session_project: str | None) -> Result:
    """Delete a memory."""
    return drop_record(
        args,
        session_project,
        "memory_id",
        "memory",
        MEMORY_PROJECT,
        """WITH gone AS (
             DELETE FROM nodes
              WHERE project = $1 AND id = $2 AND type = 'memory'
          RETURNING project, id, name
           ), links AS (
             DELETE FROM record_links AS r USING gone
              WHERE r.record_project = gone.project AND r.record_id = gone.id
           )
           SELECT name FROM gone""",
    )


def save_suggestion(args: Args, session_project: str | None) -> Result:
    """Record a gap of the tooling, or count another sighting of one."""
    suggestion_id = require_string(args, "suggestion_id")
    title = require_string(args, "title")
    detail = require_string(args, "detail")
    gist = read_optional_string(args, "summary") or title
    kind = read_optional_string(args, "kind")
    lever = read_optional_string(args, "lever")
    status = read_optional_string(args, "status")
    bump = 0 if given(args, "bump") is False else 1
    explicit, about = read_record_scope(args, session_project)

    if not explicit and about is None:
        raise ToolError(
            'Argument "about" is required: this session was opened on /mcp '
            'without naming a project. Name the project this gap is in, or "*" '
            "for one that belongs to none in particular."
        )
    if "/" in suggestion_id:
        raise ToolError(
            'Argument "suggestion_id" is a slug, not a path: the scope is '
            'taken from "about" and prefixed automatically.'
        )

    node_id = scoped_record_id(about, suggestion_id)
    asked = read_optional_string(args, "query")
    refs = parse_node_refs(given(args, "nodes"), about or session_project)
    if refs is not None:
        require_nodes(refs)
    with db.transaction() as client:
        client.query(
            """INSERT INTO projects (name, root_path, type)
             VALUES ($1, 'suggestions://agent', 'suggestions')
             ON CONFLICT (name) DO NOTHING""",
            [SUGGESTIONS_PROJECT],
        )
        # The tail object is what makes a repeat report accumulate instead of
        # overwrite. Reopening on a bump is deliberate: a gap hit again is open.
        saved = client.query(
            """INSERT INTO nodes (
               project, id, name, type, summary, content, metadata
             )
             VALUES ($1, $2, $3, 'suggestion', $4, $5,
                     JSONB_BUILD_OBJECT(
                       'about', $6::text,
                       'kind', $7::text,
                       'lever', $8::text,
                       'status', COALESCE($9::text, 'open'),
                       'hits', 1,
                       'queries', CASE WHEN $11::text IS NULL
                                       THEN '[]'::jsonb
                                       ELSE jsonb_build_array($11::text)
                                  END,
                       'first_seen', to_char(
                         now() AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                       ),
                       'last_seen', to_char(
                         now() AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                       )
                     ))
             ON CONFLICT (project, id) DO UPDATE SET
               name = EXCLUDED.name,
               type = 'suggestion',
               summary = EXCLUDED.summary,
               content = EXCLUDED.content,
               metadata = nodes.metadata
                 || EXCLUDED.metadata
                 || JSONB_BUILD_OBJECT(
                      'kind',
                      COALESCE($7::text, nodes.metadata ->> 'kind'),
                      'lever',
                      COALESCE($8::text, nodes.metadata ->> 'lever'),
                      'status',
                      COALESCE($9::text,
                               CASE WHEN $10::int > 0 THEN 'open' END,
                               nodes.metadata ->> 'status',
                               'open'),
                      'first_seen',
                      COALESCE(nodes.metadata ->> 'first_seen',
                               EXCLUDED.metadata ->> 'first_seen'),
                      'hits',
                      COALESCE((nodes.metadata ->> 'hits')::int, 0)
                        + $10::int,
                      'queries',
                      CASE
                        WHEN $11::text IS NULL
                          OR COALESCE(nodes.metadata -> 'queries',
                                      '[]'::jsonb) ? $11::text
                        THEN COALESCE(nodes.metadata -> 'queries',
                                      '[]'::jsonb)
                        WHEN jsonb_array_length(
                               nodes.metadata -> 'queries') >= 20
                        THEN (nodes.metadata -> 'queries') - 0
                               || jsonb_build_array($11::text)
                        ELSE COALESCE(nodes.metadata -> 'queries',
                                      '[]'::jsonb)
                               || jsonb_build_array($11::text)
                      END
                    )
             RETURNING (metadata ->> 'hits')::int AS hits,
                       metadata ->> 'status' AS status,
                       (xmax = 0) AS created""",
            [
                SUGGESTIONS_PROJECT,
                node_id,
                title,
                gist,
                detail,
                about,
                kind,
                lever,
                status,
                bump,
                asked,
            ],
        )[0]
        if refs is not None:
            replace_record_nodes(client, SUGGESTIONS_PROJECT, node_id, refs)

    where = "no project in particular" if about is None else about
    verb = "recorded" if saved["created"] else "updated"
    lines = [
        f"Suggestion {node_id} {verb} (about {where}, "
        f"{saved['status']}, hits {saved['hits']})."
    ]
    lines += unknown_scope_note(about, "suggestion", "read")
    return text_result("\n".join(lines))


def get_suggestions(args: Args, session_project: str | None) -> Result:
    """List the gaps of a scope, or roll them up by a field."""
    _, scope = read_record_scope(args, session_project)
    wanted = read_optional_string(args, "suggestion_id")
    named = read_optional_string(args, "status")
    status = None if named == "*" else (named or "open")
    kind = read_optional_string(args, "kind")
    query = read_optional_string(args, "query")

    about = expand_record_scope(scope)
    group_by = read_optional_string(args, "group_by")
    if group_by is not None:
        if group_by not in SUGGESTION_GROUPS:
            raise ToolError(f"group_by must be one of {', '.join(SUGGESTION_GROUPS)}")
        groups = group_suggestions(SUGGESTIONS_PROJECT, group_by, about, status, kind)
        return json_result({"group_by": group_by, "groups": groups})
    attached = attached_to(
        SUGGESTIONS_PROJECT, read_node_filter(args, scope or session_project)
    )
    ids = narrow_ids(record_ids(wanted, about), attached)
    rows = db.query(
        """SELECT id, name AS title, summary, content AS detail,
                  metadata ->> 'about' AS about,
                  metadata ->> 'kind' AS kind,
                  metadata ->> 'lever' AS lever,
                  metadata ->> 'status' AS status,
                  COALESCE((metadata ->> 'hits')::int, 0) AS hits,
                  metadata ->> 'first_seen' AS first_seen,
                  metadata ->> 'last_seen' AS last_seen,
                  COALESCE(metadata -> 'queries', '[]'::jsonb) AS queries
             FROM nodes
            WHERE project = $1
              AND type = 'suggestion'
              AND ($2::text[] IS NULL OR id = ANY ($2))
              AND ($3::text[] IS NULL
                   OR metadata ->> 'about' = ANY ($3)
                   OR metadata ->> 'about' IS NULL)
              AND ($4::text IS NULL OR metadata ->> 'status' = $4)
              AND ($5::text IS NULL OR metadata ->> 'kind' = $5)
              AND ($6::text IS NULL
                   OR name ILIKE $6 OR summary ILIKE $6 OR content ILIKE $6)
            ORDER BY COALESCE((metadata ->> 'hits')::int, 0) DESC,
                     metadata ->> 'last_seen' DESC NULLS LAST
            LIMIT $7""",
        [
            SUGGESTIONS_PROJECT,
            ids,
            about,
            status,
            kind,
            None if query is None else f"%{query}%",
            read_limit(args),
        ],
    )
    return json_result(with_nodes(SUGGESTIONS_PROJECT, rows))


def drop_suggestion(args: Args, session_project: str | None) -> Result:
    """Delete a suggestion."""
    return drop_record(
        args,
        session_project,
        "suggestion_id",
        "suggestion",
        SUGGESTIONS_PROJECT,
        """WITH gone AS (
             DELETE FROM nodes
              WHERE project = $1 AND id = $2 AND type = 'suggestion'
          RETURNING project, id, name
           ), links AS (
             DELETE FROM record_links AS r USING gone
              WHERE r.record_project = gone.project AND r.record_id = gone.id
           )
           SELECT name FROM gone""",
    )


def describe_project_tool(args: Args, session_project: str | None) -> Result:
    """Say what a project is, or which project reads a host directory."""
    path = read_optional_string(args, "path")
    matched: str | None = None

    if path is None:
        target = require_project(read_project(args, session_project))
    else:
        # Longest root wins, on a path boundary: a directory inside a project
        # is that project's, and `/src/beta-old` is not `beta`.
        wanted = re.sub(r"/+\Z", "", path) or "/"
        owner = db.query(
            """SELECT p.name AS project, p.root_path
               FROM projects AS p
              WHERE $1 = p.root_path OR starts_with($1, p.root_path || '/')
              ORDER BY length(p.root_path) DESC
              LIMIT 1""",
            [wanted],
        )
        if not owner:
            return text_result(
                f"No indexed tree contains {wanted}. "
                "list_projects names the tree every project reads; "
                "onboard this one with `make install`."
            )
        target = owner[0]["project"]
        matched = owner[0]["root_path"]

    profile = describe_project(target)
    answer: dict[str, Any] = dict(profile)
    if matched is not None:
        answer["matched_tree"] = matched

    if profile["type"] == ORGANIZATION_TYPE:
        answer["members"] = describe_members(target)
        answer["note"] = (
            f"{target} is an organization: it holds the projects listed "
            "here rather than a tree of its own. Every read naming it - "
            "search_code_nodes, the graph reads, the plans, the memories, "
            "the suggestions - covers all of them at once; name a member "
            "instead to read just that one, and to write anything."
        )
    answer["organizations"] = describe_holders(target)
    members = read_scope(target)["members"]
    answer["links"] = link_summary(members)
    answer["knowledge"] = knowledge_counts(members)
    return json_result(answer)


def search_code_nodes(args: Args, session_project: str | None) -> Result:
    """Find nodes by name or id, in one project or across many."""
    pattern = f"%{require_string(args, 'query')}%"
    limit = read_limit(args)
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

    # Round robin, not concatenation: each project's first hit, then each
    # project's second, so one project cannot spend the whole limit.
    found = db.query(
        """WITH scope AS (
             SELECT p.name, p.type FROM projects AS p
              WHERE ($1::text IS NULL
                     OR p.name = $1
                     OR EXISTS (
                          SELECT 1 FROM org_members AS m
                           WHERE m.organization = $1 AND m.project = p.name
                        ))
                AND ($2::text IS NULL OR p.type = $2)
           ),
           hits AS (
             SELECT n.project, s.type AS project_type, n.id, n.name, n.type,
                    n.file_path, n.summary,
                    ROW_NUMBER() OVER (
                      PARTITION BY n.project ORDER BY n.id
                    ) AS rn
               FROM nodes AS n
               JOIN scope AS s ON s.name = n.project
              WHERE n.name ILIKE $3 OR n.id ILIKE $3
           )
           SELECT project, project_type, id, name, type, file_path, summary
             FROM hits
            ORDER BY rn, project, id
            LIMIT $4""",
        [named, kind, pattern, limit],
    )

    # A single project keeps the shape it always had; across projects the
    # rows are regrouped, since a reader wants them by project.
    spread = len({row["project"] for row in found}) > 1
    if named is None or spread:
        rows = sorted(
            found,
            key=lambda row: (
                collate.key(str(row["project"])),
                collate.key(str(row["id"])),
            ),
        )
    else:
        rows = [
            {
                key: value
                for key, value in row.items()
                if key not in ("project", "project_type")
            }
            for row in found
        ]

    if not rows and kind is not None:
        types = db.query("""SELECT DISTINCT type FROM projects ORDER BY type""")
        known = ", ".join(row["type"] for row in types)
        return text_result(
            "No node matching the query in any project of type "
            f'"{kind}". Types in this database: {known or "none"}.'
        )
    return json_result(rows)


def search_code(args: Args, session_project: str | None) -> Result:
    """Search by words and by meaning, fused and reranked."""
    query = require_string(args, "query")
    limit = read_limit(args)
    named, kind = read_search_scope(args, session_project)
    use_rerank = given(args, "rerank") is not False
    mode = read_optional_string(args, "mode")
    if mode is not None and mode not in MODES:
        raise ToolError(f"mode must be one of {', '.join(MODES)}")

    found = hybrid_search(named, kind, query, limit, mode=mode or "hybrid")
    if mode == "vector" and not found["vectorAvailable"]:
        raise ToolError(
            "mode vector cannot answer: no embedding server answered for the "
            "query. Embedding is off or its server is down; mode lexical "
            "answers without it."
        )
    if mode == "vector" and not found["embedded"]:
        raise ToolError(
            "mode vector cannot answer: nothing in scope has embeddings yet."
        )
    ranked = rerank(found["rows"], query, limit, use_rerank)
    # The project columns are noise when every row carries the same two values.
    spread = len({item["row"]["project"] for item in ranked}) > 1
    rows = []
    for item in ranked:
        row = item["row"]
        shown: dict[str, Any] = {}
        if named is None or spread:
            shown["project"] = row["project"]
            shown["project_type"] = row["project_type"]
        shown.update(
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "file_path": row["file_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "score": jsjson.fixed(item["score"], 5),
                "rrf": jsjson.fixed(row["rrf"], 5),
                "lexical_rank": row["lexical_rank"],
                "vector_rank": row["vector_rank"],
                "summary": row["summary"],
                "snippet": row["snippet"],
            }
        )
        if row.get("matched") == "summary":
            shown["matched"] = "summary"
        rows.append(shown)

    note = semantic_note(found) if mode is None else mode_note(mode, found)
    text = jsjson.dumps(rows, 2)
    return text_result(text if note is None else f"{note}\n\n{text}")


def get_context(args: Args, session_project: str | None) -> Result:
    """Answer a broad question with one budgeted packet."""
    query = require_string(args, "query")
    named, kind = read_search_scope(args, session_project)
    packet = build_context(
        query,
        named,
        kind,
        read_bounded(args, "seeds", DEFAULT_SEEDS, 1, MAX_SEEDS),
        read_bounded(
            args,
            "token_budget",
            DEFAULT_TOKEN_BUDGET,
            MIN_TOKEN_BUDGET,
            MAX_TOKEN_BUDGET,
        ),
        read_expand(args),
        given(args, "include_chunks") is not False,
        given(args, "rerank") is not False,
        read_detail(args),
    )
    return json_result(packet)


def search_text(args: Args, session_project: str | None) -> Result:
    """Find a string in the files themselves, the way grep does."""
    named, kind = read_search_scope(args, session_project)
    scope = db.query(
        """SELECT p.name FROM projects AS p
            WHERE p.type NOT IN ('organization', 'memory', 'suggestions',
                                 'plans')
              AND ($1::text IS NULL
                   OR p.name = $1
                   OR EXISTS (
                        SELECT 1 FROM org_members AS m
                         WHERE m.organization = $1 AND m.project = p.name
                      ))
              AND ($2::text IS NULL OR p.type = $2)
            ORDER BY p.name""",
        [named, kind],
    )
    projects = [row["name"] for row in scope]
    if not projects:
        return text_result("No indexed project to search.")
    found = grep_trees(
        {
            "projects": projects,
            "pattern": require_string(args, "pattern"),
            "regex": given(args, "regex") is True,
            "loose": given(args, "loose") is True,
            "path": read_optional_string(args, "path") or "",
            "limit": read_bounded(args, "limit", DEFAULT_TEXT_LINES, 1, MAX_TEXT_LINES),
        }
    )
    return json_result(found)


def find_linked_name(args: Args, session_project: str | None) -> Result:
    """Say who provides and who takes a name across projects."""
    named, kind = read_search_scope(args, session_project)
    link_kind = read_optional_string(args, "kind")
    if link_kind is not None and link_kind not in LINK_KINDS:
        raise ToolError(f"kind must be one of {', '.join(LINK_KINDS)}")
    return json_result(find_name(require_string(args, "name"), named, kind, link_kind))


def get_code_graph_neighbors(args: Args, scope: dict[str, Any]) -> Result:
    """List what a node is tied to: edges, links to other projects, records."""
    targets: list[str] = scope["members"]
    node_id = require_string(args, "node_id")
    # The project is carried through the CTE rather than fixed: joining on
    # the scope would pair a member's edge with another member's node.
    found = db.query(
        """WITH neighbours AS (
           SELECT project, target_id AS node_id, relation_type,
                  'outgoing' AS direction
             FROM edges WHERE project = ANY ($1) AND source_id = $2
           UNION
           SELECT project, source_id AS node_id, relation_type,
                  'incoming' AS direction
             FROM edges WHERE project = ANY ($1) AND target_id = $2
         )
         SELECT n.project, n.node_id, n.relation_type, n.direction,
                g.type, g.file_path, g.summary
           FROM neighbours AS n
           LEFT JOIN nodes AS g
             ON g.project = n.project AND g.id = n.node_id
          ORDER BY n.project, n.direction, n.relation_type, n.node_id
          LIMIT $3""",
        [targets, node_id, MAX_RESULTS],
    )
    rows = found
    if not scope["organization"]:
        rows = [
            {key: value for key, value in row.items() if key != "project"}
            for row in found
        ]
    # A link names the other project on every row: it is the answer.
    linked = []
    for link in node_links(targets, node_id):
        outgoing = link["source_project"] in targets and link["source_id"] == node_id
        linked.append(
            {
                "project": link["target_project" if outgoing else "source_project"],
                "node_id": link["target_id" if outgoing else "source_id"],
                "relation_type": link["relation_type"],
                "direction": "outgoing" if outgoing else "incoming",
                "link": {
                    "kind": link["kind"],
                    "name": link["name"],
                    "origin": link["origin"],
                },
            }
        )
    known = [
        {
            "node_id": record["record_id"],
            "relation_type": record["relation"],
            "direction": "knowledge",
            "type": record["record_type"],
            "summary": record["summary"]
            if record["summary"] is not None
            else record["title"],
            "knowledge": {
                "project": record["record_project"],
                "title": record["title"],
                "status": record["status"],
                "attached_to": record["node_id"],
            },
        }
        for record in knowledge_for(
            [{"project": one, "node_id": node_id} for one in targets]
        )
    ]
    return json_result([*rows, *linked, *known])


def get_project_links(args: Args, scope: dict[str, Any]) -> Result:
    """Say how projects reach each other around the ones named."""
    direction = read_optional_string(args, "direction") or "both"
    if direction not in LINK_DIRECTIONS:
        raise ToolError(f"direction must be one of {', '.join(LINK_DIRECTIONS)}")
    return json_result(
        project_links(
            scope["members"],
            direction,
            read_bounded(args, "depth", DEFAULT_LINK_DEPTH, 1, MAX_LINK_DEPTH),
            read_optional_string(args, "relation"),
            read_optional_string(args, "node_id"),
        )
    )


def trace(args: Args, scope: dict[str, Any]) -> Result:
    """Follow one node across projects."""
    if scope["organization"]:
        raise ToolError(
            f"{scope['project']} is an organization; a trace starts at a node "
            "of one project, so name the member it belongs to"
        )
    node_id = require_string(args, "node_id")
    return json_result(
        trace_node(
            {"project": scope["members"][0], "id": node_id},
            read_bounded(args, "max_steps", DEFAULT_TRACE_STEPS, 1, MAX_TRACE_STEPS),
        )
    )


def project_link_tools(name: str, args: Args, scope: dict[str, Any]) -> Result:
    """Declare a relation between two projects, or take one back."""
    if scope["organization"]:
        return write_needs_member(scope, "link from")
    relation = {
        "source_project": scope["project"],
        "source_id": read_optional_string(args, "source_id") or "./",
        "target_project": require_project(require_string(args, "target_project")),
        "target_id": read_optional_string(args, "target_id") or "./",
        "relation": require_string(args, "relation"),
    }
    described = (
        f"{relation['source_project']}:{relation['source_id']} "
        f"{relation['relation']} {relation['target_project']}:{relation['target_id']}"
    )
    if name == "drop_project_link":
        dropped = drop_relation(relation)
        return text_result(
            f"No declared relation {described}."
            if dropped == 0
            else f"Dropped {described}."
        )
    if relation["source_project"] == relation["target_project"]:
        raise ToolError("A relation inside one project is a graph edge, not a link.")
    ends = [
        (relation["source_project"], relation["source_id"]),
        (relation["target_project"], relation["target_id"]),
    ]
    for where, node_id in ends:
        if node_id != "./" and not node_exists(where, node_id):
            raise ToolError(
                f"{where} has no node {node_id}; search_code_nodes finds the id."
            )
    save_relation(relation, read_optional_string(args, "note"))
    return text_result(f"Saved {described}.")


def project_export_tools(name: str, args: Args, scope: dict[str, Any]) -> Result:
    """Say that a project provides a name, or take that back."""
    if scope["organization"]:
        return write_needs_member(scope, "export from")
    project = scope["project"]
    kind = require_string(args, "kind")
    if kind not in LINK_KINDS:
        raise ToolError(f"kind must be one of {', '.join(LINK_KINDS)}")
    raw = require_string(args, "name")
    exported = normalize_name(kind, raw)
    if exported == "":
        raise ToolError(f'"{raw}" names nothing once normalized.')
    if name == "drop_project_export":
        said = {
            "dropped": f"Dropped {kind} {exported} from {project}.",
            "auto": f"{project} provides {kind} {exported} by a manifest "
            "of its tree; it stays until the tree stops declaring it.",
            "missing": f"{project} does not provide {kind} {exported}.",
        }
        return text_result(said[drop_export(project, kind, exported)])
    node_id = read_optional_string(args, "node_id") or "./"
    if node_id != "./" and not node_exists(project, node_id):
        raise ToolError(f"{project} has no node {node_id}.")
    save_export(project, kind, exported, node_id)
    return text_result(
        f"{project} provides {kind} {exported} at {node_id}; "
        "every project taking that name is linked to it."
    )


def shortest_path(args: Args, scope: dict[str, Any]) -> Result:
    """Find the shortest walk between two nodes of one project."""
    source_id = require_string(args, "source_id")
    target_id = require_string(args, "target_id")

    # Breadth first, in the database, edges followed both ways. The walk is
    # held to the project it started in: two members share node ids.
    rows = db.query(
        """WITH RECURSIVE walk(project, node_id, path, depth) AS (
             SELECT member, $2::VARCHAR, ARRAY[$2::VARCHAR], 0
               FROM unnest($1::VARCHAR[]) AS member
           UNION ALL
             SELECT walk.project, next.id, walk.path || next.id, walk.depth + 1
               FROM walk
               JOIN LATERAL (
                 SELECT CASE
                          WHEN e.source_id = walk.node_id THEN e.target_id
                          ELSE e.source_id
                        END AS id
                   FROM edges e
                  WHERE e.project = walk.project
                    AND (e.source_id = walk.node_id
                      OR e.target_id = walk.node_id)
               ) AS next ON TRUE
              WHERE walk.depth < $4
                AND walk.node_id <> $3
                AND NOT (next.id = ANY (walk.path))
         )
         SELECT project, path, depth
           FROM walk
          WHERE node_id = $3
          ORDER BY depth, project
          LIMIT 1""",
        [scope["members"], source_id, target_id, read_hops(args)],
    )
    if not rows:
        where = (
            f"any project {scope['project']} holds"
            if scope["organization"]
            else scope["project"]
        )
        return text_result(
            f"No path from {source_id} to {target_id} within the hop limit, in {where}"
        )
    walked = rows[0]
    if scope["organization"]:
        return json_result(walked)
    return json_result({"path": walked["path"], "depth": walked["depth"]})


def without_key(value: Any, dropped: str) -> Any:  # noqa: ANN401
    """Return a value with one key removed at every depth."""
    if isinstance(value, dict):
        return {
            key: without_key(item, dropped)
            for key, item in value.items()
            if key != dropped
        }
    if isinstance(value, list):
        return [without_key(item, dropped) for item in value]
    return value


def symbol_tools(name: str, args: Args, scope: dict[str, Any]) -> Result:
    """Answer a question about one symbol."""
    targets: list[str] = scope["members"]
    symbol = require_string(args, "symbol")
    file_path = read_optional_string(args, "file_path")
    if name == "find_definition":
        answer = find_definition(targets, symbol, file_path)
    elif name == "impact_analysis":
        depth = read_bounded(args, "depth", DEFAULT_IMPACT_DEPTH, 1, MAX_SYMBOL_HOPS)
        answer = impact_analysis(targets, symbol, file_path, depth)
    else:
        hops = read_bounded(args, "max_hops", DEFAULT_SYMBOL_HOPS, 1, MAX_SYMBOL_HOPS)
        answer = find_symbol(targets, name, symbol, file_path, hops)
    # The project is a column only across an organization, as elsewhere.
    if not scope["organization"]:
        answer = without_key(answer, "project")
    return json_result(answer)


def save_node_summary(args: Args, scope: dict[str, Any]) -> Result:
    """Write a summary by hand, which no index run overwrites."""
    if scope["organization"]:
        return write_needs_member(scope, "summarise a node in")
    node_id = require_string(args, "node_id")
    summary = require_string(args, "summary")
    name = node_id.split("/")[-1] or node_id
    # Tagged manual so the indexer leaves it alone.
    db.query(
        """INSERT INTO nodes (project, id, name, type, summary, metadata)
         VALUES ($1, $2, $3, $4, $5, '{"summary_source": "manual"}'::jsonb)
         ON CONFLICT (project, id) DO UPDATE SET
           summary = EXCLUDED.summary,
           metadata = nodes.metadata
             || '{"summary_source": "manual"}'::jsonb""",
        [scope["project"], node_id, name, "file", summary],
    )
    return text_result(f"Summary successfully saved for node: {node_id}")


def member_rows(rows: list[db.Row], scope: dict[str, Any]) -> list[db.Row]:
    """Drop the project column unless the rows come from several members."""
    if scope["organization"]:
        return rows
    return [
        {key: value for key, value in row.items() if key != "project"} for row in rows
    ]


def get_node_summary(args: Args, scope: dict[str, Any]) -> Result:
    """Return the summary of a node, in every member that has one."""
    node_id = require_string(args, "node_id")
    rows = db.query(
        """SELECT project, id, summary, file_path, type
           FROM nodes
          WHERE project = ANY ($1) AND id = $2
          ORDER BY project""",
        [scope["members"], node_id],
    )
    return json_result(member_rows(rows, scope))


def get_overview(args: Args, scope: dict[str, Any]) -> Result:
    """Return the summary tree below a directory."""
    path = given(args, "path")
    overview = build_overview(
        scope["members"],
        path if isinstance(path, str) else "",
        read_bounded(args, "depth", DEFAULT_OVERVIEW_DEPTH, 0, MAX_OVERVIEW_DEPTH),
        given(args, "include_entities") is True,
        read_bounded(
            args,
            "token_budget",
            DEFAULT_OVERVIEW_BUDGET,
            MIN_TOKEN_BUDGET,
            MAX_TOKEN_BUDGET,
        ),
    )
    if not scope["organization"]:
        overview = without_key(overview, "project")
    return json_result(overview)


def get_file_hash(args: Args, scope: dict[str, Any]) -> Result:
    """Return the hash a file had when it was last indexed."""
    rel_path = require_string(args, "rel_path")
    rows = db.query(
        """SELECT project, hash, updated_at
           FROM indexed_files
          WHERE project = ANY ($1) AND file_path = $2
          ORDER BY project""",
        [scope["members"], rel_path],
    )
    return json_result(member_rows(rows, scope))


def clear_file_hash(args: Args, scope: dict[str, Any]) -> Result:
    """Forget a file's hash, so the next index run reads it again."""
    if scope["organization"]:
        return write_needs_member(scope, "clear a hash in")
    rel_path = require_string(args, "rel_path")
    db.query(
        "DELETE FROM indexed_files WHERE project = $1 AND file_path = $2",
        [scope["project"], rel_path],
    )
    return text_result(
        f"Hash cleared for file: {rel_path}. Re-indexing will now pick it up."
    )


def set_file_hash(args: Args, scope: dict[str, Any]) -> Result:
    """Record a file's hash."""
    if scope["organization"]:
        return write_needs_member(scope, "record a hash in")
    rel_path = require_string(args, "rel_path")
    file_hash = require_string(args, "hash")
    db.query(
        """INSERT INTO indexed_files (project, file_path, hash, updated_at)
         VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
         ON CONFLICT (project, file_path) DO UPDATE SET
           hash = EXCLUDED.hash,
           updated_at = CURRENT_TIMESTAMP""",
        [scope["project"], rel_path, file_hash],
    )
    return text_result(f"Hash set for file: {rel_path}.")


def date_string(value: Any) -> str:  # noqa: ANN401
    """Spell a timestamp as JavaScript's String(date) does, in UTC."""
    if not isinstance(value, datetime):
        return str(value)
    moment = value.astimezone(UTC) if value.tzinfo else value
    zone = "GMT+0000 (Coordinated Universal Time)"
    return moment.strftime("%a %b %d %Y %H:%M:%S ") + zone


def list_indexed_files(args: Args, scope: dict[str, Any]) -> Result:
    """List the files an index run recorded."""
    if not scope["organization"]:
        return json_result(
            db.query(
                """SELECT file_path, hash, updated_at
                 FROM indexed_files
                WHERE project = $1
                ORDER BY updated_at DESC""",
                [scope["project"]],
            )
        )
    # An organization takes a turn from each member, or one large member
    # would be the whole answer.
    rows = db.query(
        """WITH listed AS (
                 SELECT project, file_path, hash, updated_at,
                        ROW_NUMBER() OVER (
                          PARTITION BY project ORDER BY updated_at DESC
                        ) AS rn
                   FROM indexed_files
                  WHERE project = ANY ($1)
               )
               SELECT project, file_path, hash, updated_at
                 FROM listed
                ORDER BY rn, project
                LIMIT $2""",
        [scope["members"], MAX_RESULTS],
    )
    # Regrouped by member. Within one the previous server compared the
    # dates as text, newest spelling first, and that order is kept.
    rows.sort(key=lambda row: collate.key(date_string(row["updated_at"])), reverse=True)
    rows.sort(key=lambda row: collate.key(str(row["project"])))
    return json_result(rows)


Handler = Callable[[Args, Any], Result]

# Answered before any project is looked up: these name their own scope, span
# the database, or work on a built-in project.
EARLY: dict[str, Handler] = {
    "list_projects": list_projects,
    "drop_project": drop_project_tool,
    "save_plan": save_plan,
    "get_plans": get_plans,
    "drop_plan": drop_plan,
    "save_memory": save_memory,
    "get_memory": get_memory,
    "drop_memory": drop_memory,
    "save_suggestion": save_suggestion,
    "get_suggestions": get_suggestions,
    "drop_suggestion": drop_suggestion,
    "describe_project": describe_project_tool,
    "search_code_nodes": search_code_nodes,
    "search_code": search_code,
    "get_context": get_context,
    "search_text": search_text,
    "find_linked_name": find_linked_name,
}

SYMBOL_TOOLS = (
    "find_definition",
    "find_callers",
    "find_callees",
    "find_references",
    "find_implementations",
    "find_tests",
    "impact_analysis",
)
LINK_TOOLS = ("save_project_link", "drop_project_link")
EXPORT_TOOLS = ("save_project_export", "drop_project_export")
SKILL_TOOLS = ("list_skills", "get_skill")

# Reads that say so when the organization they name holds nothing.
SCOPED: dict[str, tuple[Handler, bool]] = {
    "get_code_graph_neighbors": (get_code_graph_neighbors, True),
    "get_project_links": (get_project_links, True),
    "trace": (trace, False),
    "shortest_path": (shortest_path, True),
    "save_node_summary": (save_node_summary, False),
    "get_node_summary": (get_node_summary, True),
    "get_overview": (get_overview, True),
    "get_file_hash": (get_file_hash, True),
    "clear_file_hash": (clear_file_hash, False),
    "set_file_hash": (set_file_hash, False),
    "list_indexed_files": (list_indexed_files, True),
}


def dispatch(name: str, args: Args, session_project: str | None) -> Result:
    """Run one tool; whatever it raises is the caller's to report."""
    if name in EARLY:
        return EARLY[name](args, session_project)
    if name in SKILL_TOOLS:
        return skill_tools(name, args, session_project)

    # Every read below covers the one project named, or the projects an
    # organization holds.
    scope = read_scope(read_project(args, session_project))
    nothing_held = scope["organization"] and not scope["members"]
    if name in LINK_TOOLS:
        return project_link_tools(name, args, scope)
    if name in EXPORT_TOOLS:
        return project_export_tools(name, args, scope)
    if name in SYMBOL_TOOLS:
        if nothing_held:
            return empty_organization(scope)
        return symbol_tools(name, args, scope)
    if name in SCOPED:
        handler, refuses_empty = SCOPED[name]
        if refuses_empty and nothing_held:
            return empty_organization(scope)
        return handler(args, scope)
    raise ToolError(f"Tool {name} not found")


def call_tool(name: str, args: Args, session_project: str | None) -> Result:
    """Run one tool and answer; an error is an answer, never a dropped session."""
    try:
        return dispatch(name, args, session_project)
    except Exception as error:  # noqa: BLE001 - reported through the result
        said = message(error)
        LOG.error("Tool %s failed: %s", name, said)
        return text_result(f"Error: {said}", True)
