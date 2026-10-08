"""Records tied to code: which nodes a memory, plan or suggestion is about."""

from __future__ import annotations

from typing import Any

from enggraph.core import db
from enggraph.mcp.errors import ToolError
from enggraph.mcp.links import ancestor_ids

DEFAULT_RELATION = "about"
MAX_NODES = 50
MAX_KNOWLEDGE = 30
MAX_TOP = 5

SUGGESTION_GROUPS = ["kind", "lever", "about", "directory"]

NodeRef = dict[str, str]


def read_ref(item: Any, fallback: str | None) -> dict[str, Any] | None:  # noqa: ANN401
    """Read one entry of `nodes`: an id, or an object naming its project."""
    if isinstance(item, str):
        return {"project": fallback, "node_id": item}
    if not isinstance(item, dict):
        return None
    project = item.get("project")
    named = project.strip() if isinstance(project, str) else ""
    return {"project": named or fallback, "node_id": item.get("node_id")}


def parse_node_refs(
    raw: Any,  # noqa: ANN401
    fallback: str | None,
) -> list[NodeRef] | None:
    """Read a `nodes` argument; absent is None, which keeps the links as they are."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ToolError('Argument "nodes" must be an array')
    refs: dict[tuple[str, str], NodeRef] = {}
    for item in raw:
        ref = read_ref(item, fallback)
        if (
            ref is None
            or not isinstance(ref["node_id"], str)
            or ref["node_id"].strip() == ""
        ):
            raise ToolError(
                'Every entry of "nodes" is a node id or { project, node_id }'
            )
        if ref["project"] is None or ref["project"] == "":
            raise ToolError(
                f"Node {ref['node_id']} names no project, and the record has none to "
                "lend it: pass { project, node_id }"
            )
        node = {"project": ref["project"], "node_id": ref["node_id"].strip()}
        refs[(node["project"], node["node_id"])] = node
    if len(refs) > MAX_NODES:
        raise ToolError(f"A record names at most {MAX_NODES} nodes")
    return list(refs.values())


def require_nodes(refs: list[NodeRef]) -> None:
    """Refuse nodes that do not exist, naming every one of them."""
    if not refs:
        return
    rows = db.query(
        """SELECT w.project, w.node_id
       FROM unnest($1::text[], $2::text[]) AS w (project, node_id)
      WHERE NOT EXISTS (
              SELECT 1 FROM nodes AS n
               WHERE n.project = w.project AND n.id = w.node_id
            )""",
        [[ref["project"] for ref in refs], [ref["node_id"] for ref in refs]],
    )
    if rows:
        missing = [f"{row['project']}:{row['node_id']}" for row in rows]
        raise ToolError(
            f"No such node: {', '.join(missing)}. Find the id with "
            "search_code_nodes; nothing was saved"
        )


def replace_record_nodes(
    client: db.Session, record_project: str, record_id: str, refs: list[NodeRef]
) -> None:
    """Make a record's links exactly `refs`, inside the caller's transaction."""
    client.query(
        "DELETE FROM record_links WHERE record_project = $1 AND record_id = $2",
        [record_project, record_id],
    )
    client.query(
        """INSERT INTO record_links (record_project, record_id, project, node_id)
     SELECT $1::text, $2::text, w.project, w.node_id
       FROM unnest($3::text[], $4::text[]) AS w (project, node_id)""",
        [
            record_project,
            record_id,
            [ref["project"] for ref in refs],
            [ref["node_id"] for ref in refs],
        ],
    )


def nodes_of_records(
    record_project: str, record_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Return the links of each record, a node that is gone marked missing."""
    found: dict[str, list[dict[str, Any]]] = {}
    if not record_ids:
        return found
    rows = db.query(
        """SELECT r.record_id, r.project, r.node_id, r.relation,
            NOT EXISTS (
              SELECT 1 FROM nodes AS n
               WHERE n.project = r.project AND n.id = r.node_id
            ) AS missing
       FROM record_links AS r
      WHERE r.record_project = $1 AND r.record_id = ANY ($2::text[])
      ORDER BY r.record_id, r.project, r.node_id""",
        [record_project, record_ids],
    )
    for row in rows:
        node = {name: value for name, value in row.items() if name != "record_id"}
        found.setdefault(row["record_id"], []).append(node)
    return found


def with_nodes(record_project: str, rows: list[db.Row]) -> list[db.Row]:
    """Add `nodes` to every row of a record listing."""
    nodes = nodes_of_records(record_project, [row["id"] for row in rows])
    return [{**row, "nodes": nodes.get(row["id"], [])} for row in rows]


def around(refs: list[NodeRef]) -> dict[str, list[str]]:
    """Return each node and every directory above it, each pointing back at it."""
    wanted: dict[str, list[str]] = {"project": [], "id": [], "via": []}
    for ref in refs:
        for one in [ref["node_id"], *ancestor_ids(ref["node_id"])]:
            wanted["project"].append(ref["project"])
            wanted["id"].append(one)
            wanted["via"].append(ref["node_id"])
    return wanted


def knowledge_for(refs: list[NodeRef], limit: int = MAX_KNOWLEDGE) -> list[db.Row]:
    """Return what the records say about nodes or the directories above them."""
    if not refs:
        return []
    wanted = around(refs)
    return db.query(
        """SELECT DISTINCT ON (r.record_project, r.record_id)
            r.record_project, r.record_id, n.type AS record_type,
            n.name AS title, n.summary,
            n.metadata ->> 'status' AS status,
            n.metadata ->> 'about' AS about,
            r.project, r.node_id, w.via, r.relation
       FROM unnest($1::text[], $2::text[], $3::text[]) AS w (project, id, via)
       JOIN record_links AS r ON r.project = w.project AND r.node_id = w.id
       JOIN nodes AS n
         ON n.project = r.record_project AND n.id = r.record_id
      ORDER BY r.record_project, r.record_id, (r.node_id = w.via) DESC
      LIMIT $4""",
        [wanted["project"], wanted["id"], wanted["via"], limit],
    )


def records_about(record_project: str, ref: NodeRef) -> list[str]:
    """Return the records of one kind attached to a node or a directory above it."""
    wanted = around([ref])
    rows = db.query(
        """SELECT DISTINCT r.record_id
       FROM record_links AS r
       JOIN unnest($2::text[], $3::text[]) AS w (project, id)
         ON r.project = w.project AND r.node_id = w.id
      WHERE r.record_project = $1""",
        [record_project, wanted["project"], wanted["id"]],
    )
    return [row["record_id"] for row in rows]


def narrow_ids(ids: list[str] | None, attached: list[str] | None) -> list[str] | None:
    """Narrow an id filter to the records on a node; None keeps it open."""
    if attached is None:
        return ids
    return attached if ids is None else [one for one in ids if one in attached]


def group_suggestions(
    record_project: str,
    by: str,
    about: list[str] | None,
    status: str | None,
    kind: str | None,
) -> list[db.Row]:
    """Roll open gaps up by a field, or by the directories their nodes sit in."""
    args: list[Any] = [record_project, about, status, kind, MAX_TOP]
    if by == "directory":
        return db.query(
            """WITH gaps AS (
         SELECT id, name, COALESCE((metadata ->> 'hits')::int, 0) AS hits
           FROM nodes
          WHERE project = $1 AND type = 'suggestion'
            AND ($2::text[] IS NULL
                 OR metadata ->> 'about' = ANY ($2)
                 OR metadata ->> 'about' IS NULL)
            AND ($3::text IS NULL OR metadata ->> 'status' = $3)
            AND ($4::text IS NULL OR metadata ->> 'kind' = $4)
       ),
       placed AS (
         SELECT DISTINCT g.id, g.name, g.hits, r.project,
                COALESCE(
                  NULLIF(
                    substring(split_part(r.node_id, '::', 1) FROM '^(.*/)'),
                    ''
                  ),
                  './'
                ) AS directory
           FROM gaps AS g
           JOIN record_links AS r
             ON r.record_project = $1 AND r.record_id = g.id
       )
       SELECT directory AS key, project, count(*)::int AS records,
              sum(hits)::int AS hits,
              (array_agg(
                 jsonb_build_object('id', id, 'title', name, 'hits', hits)
                 ORDER BY hits DESC, id
               ))[1:$5] AS top
         FROM placed
        GROUP BY project, directory
        ORDER BY hits DESC, records DESC, project, directory""",
            args,
        )
    return db.query(
        """SELECT COALESCE(metadata ->> $6, '(none)') AS key,
            NULL::text AS project, count(*)::int AS records,
            sum(COALESCE((metadata ->> 'hits')::int, 0))::int AS hits,
            (array_agg(
               jsonb_build_object(
                 'id', id, 'title', name,
                 'hits', COALESCE((metadata ->> 'hits')::int, 0)
               )
               ORDER BY COALESCE((metadata ->> 'hits')::int, 0) DESC, id
             ))[1:$5] AS top
       FROM nodes
      WHERE project = $1 AND type = 'suggestion'
        AND ($2::text[] IS NULL
             OR metadata ->> 'about' = ANY ($2)
             OR metadata ->> 'about' IS NULL)
        AND ($3::text IS NULL OR metadata ->> 'status' = $3)
        AND ($4::text IS NULL OR metadata ->> 'kind' = $4)
      GROUP BY 1
      ORDER BY hits DESC, records DESC, key""",
        [*args, by],
    )


def knowledge_counts(projects: list[str]) -> dict[str, int]:
    """Count the records of each type attached to nodes of these projects."""
    rows = db.query(
        """SELECT n.type,
            count(DISTINCT (r.record_project, r.record_id))::int AS count
       FROM record_links AS r
       JOIN nodes AS n
         ON n.project = r.record_project AND n.id = r.record_id
      WHERE r.project = ANY ($1::text[])
      GROUP BY n.type
      ORDER BY n.type""",
        [projects],
    )
    return {row["type"]: row["count"] for row in rows}
