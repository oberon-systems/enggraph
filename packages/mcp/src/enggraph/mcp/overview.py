"""The summary tree of a project: directories, files and what they hold."""

from __future__ import annotations

import re
from typing import Any

from enggraph.mcp import collate, db, jsjson
from enggraph.mcp.context import estimate_tokens

ROOT_ID = "./"
DEFAULT_OVERVIEW_DEPTH = 2
MAX_OVERVIEW_DEPTH = 6
DEFAULT_OVERVIEW_BUDGET = 4000
# A tree deeper or wider than this is cut by the budget long before it ends.
MAX_OVERVIEW_ROWS = 3000

LEADING_DOT_SLASH = re.compile(r"\A\./(?=.)", re.DOTALL)


def candidate_ids(path: str) -> list[str]:
    """Read "", "." and "/" as the root, and a directory with or without its slash."""
    trimmed = LEADING_DOT_SLASH.sub("", path.strip())
    if trimmed in ("", ".", "/", ROOT_ID):
        return [ROOT_ID]
    if trimmed.endswith("/"):
        return [trimmed, trimmed[:-1]]
    return [trimmed, f"{trimmed}/"]


def shape(rows: list[db.Row], budget: int) -> dict[str, Any]:
    """Keep rows breadth first until the budget is spent, and nest what was kept."""
    ordered = sorted(
        rows,
        key=lambda row: (
            row["depth"],
            collate.key(row["project"]),
            int(row["type"] != "directory"),
            collate.key(row["id"]),
        ),
    )
    items: dict[tuple[str, str], dict[str, Any]] = {}
    trees: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for row in ordered:
        parent = None
        if row["parent"] is not None:
            parent = items.get((row["project"], row["parent"]))
        if row["parent"] is not None and parent is None:
            truncated = True
            continue
        item: dict[str, Any] = {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "summary": row["summary"],
            "summary_source": row["summary_source"],
            "children": row["children"],
        }
        price = estimate_tokens(jsjson.dumps(item))
        if row["parent"] is not None and used + price > budget:
            truncated = True
            continue
        used += price
        items[(row["project"], row["id"])] = item
        if parent is None:
            trees.append({"project": row["project"], "root": item})
        else:
            parent.setdefault("items", []).append(item)
    return {"trees": trees, "used": used, "truncated": truncated}


OVERVIEW = """WITH RECURSIVE start AS (
       SELECT DISTINCT ON (n.project) n.project, n.id
         FROM nodes AS n
         JOIN UNNEST($2::text[]) WITH ORDINALITY AS c(id, pick) ON c.id = n.id
        WHERE n.project = ANY ($1::text[])
        ORDER BY n.project, c.pick
     ),
     down AS (
       SELECT s.project, s.id, NULL::text AS parent, 0 AS depth
         FROM start AS s
       UNION ALL
       SELECT d.project, e.target_id, d.id, d.depth + 1
         FROM down AS d
         JOIN edges AS e
           ON e.project = d.project AND e.source_id = d.id
          AND e.relation_type = 'contains'
         JOIN nodes AS c ON c.project = e.project AND c.id = e.target_id
        WHERE d.depth < $3
          AND ($4 OR c.type IN ('directory', 'file'))
     )
     SELECT d.project, d.id, d.parent, d.depth, n.name, n.type, n.summary,
            n.metadata ->> 'summary_source' AS summary_source,
            (SELECT COUNT(*)::int
               FROM edges AS e
              WHERE e.project = d.project AND e.source_id = d.id
                AND e.relation_type = 'contains') AS children
       FROM down AS d
       JOIN nodes AS n ON n.project = d.project AND n.id = d.id
      ORDER BY d.depth, d.project, d.id
      LIMIT $5"""


def build_overview(
    projects: list[str],
    path: str,
    depth: int,
    include_entities: bool,
    token_budget: int,
) -> dict[str, Any]:
    """Walk `contains` down from a directory or file node, summaries only."""
    rows = db.query(
        OVERVIEW,
        [projects, candidate_ids(path), depth, include_entities, MAX_OVERVIEW_ROWS],
    )
    shaped = shape(rows, token_budget)
    notes: list[str] = []
    if not shaped["trees"]:
        notes.append(
            f"No node {path or ROOT_ID} in scope. Directory ids end in a "
            'slash ("src/"), the repository is "./"; a project indexed before '
            "directories existed gets them on its next index run."
        )
    if shaped["truncated"]:
        notes.append(
            "Budget spent: deeper items were left out. Drill down by calling "
            "get_overview on a listed directory, or raise token_budget."
        )
    return {
        "path": path or ROOT_ID,
        "depth": depth,
        "budget": {
            "limit": token_budget,
            "used": shaped["used"],
            "truncated": shaped["truncated"],
        },
        "trees": shaped["trees"],
        "notes": notes,
    }
