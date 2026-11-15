"""Follow one node across projects: what takes it, applies it and uses it."""

from __future__ import annotations

from typing import Any

from enggraph.core import db
from enggraph.mcp.links import ancestor_ids

DEFAULT_TRACE_STEPS = 60
MAX_TRACE_STEPS = 300
MAX_CHAINS = 30
# One more than a walk may hold, so a query that fills it has been cut short.
ROWS = MAX_TRACE_STEPS + 1

# Edges from whatever applies, deploys or configures a node to that node. The
# walk climbs them backwards: from a class to the role, from the role to the node.
APPLIED_BY = [
    "includes_class",
    "configures",
    "selects",
    "has_role",
    "includes_module",
    "requires_module",
    "uses_module",
    "uses_role",
    "includes",
]

Point = dict[str, str]
Step = dict[str, Any]


def key(point: Point) -> tuple[str, str]:
    """Return what makes two points the same point."""
    return point["project"], point["id"]


def label(point: Point) -> str:
    """Spell a point as a chain shows it."""
    return f"{point['project']}:{point['id']}"


def arrow(step: Step) -> str:
    """Spell a step as a chain shows it."""
    what = "" if step["kind"] is None else f" {step['kind']} {step['name'] or ''}"
    return f" -[{step['way']} {step['relation']}{what}]-> "


def trace_chains(start: Point, steps: list[Step]) -> list[str]:
    """Return every path from the start to a point the walk went no further from."""
    parent: dict[tuple[str, str], Step] = {}
    leaving: set[tuple[str, str]] = set()
    for step in steps:
        leaving.add(key(step["from"]))
        parent.setdefault(key(step["to"]), step)
    chains: list[str] = []
    for step in steps:
        end = key(step["to"])
        if end in leaving or parent.get(end) is not step:
            continue
        path: list[Step] = []
        current: Step | None = step
        seen: set[tuple[str, str]] = set()
        while current is not None and key(current["to"]) not in seen:
            seen.add(key(current["to"]))
            path.insert(0, current)
            current = (
                None
                if key(current["from"]) == key(start)
                else parent.get(key(current["from"]))
            )
        chains.append(
            label(start) + "".join(f"{arrow(one)}{label(one['to'])}" for one in path)
        )
        if len(chains) >= MAX_CHAINS:
            break
    return chains


def around(node_id: str) -> list[str]:
    """Return the node itself, its file and every directory above it."""
    file = node_id.split("::", 1)[0]
    return list(dict.fromkeys([node_id, file, *ancestor_ids(node_id)]))


def trace_node(
    start: Point, max_steps: int, readable: set[str] | None = None
) -> dict[str, Any]:
    """Follow one node across projects.

    From a node the walk takes what its directory provides to the projects
    that take it, climbs from what took it to whatever applies that, and
    records what those use in turn without spreading further from a used thing.
    A project outside `readable` is named where a link ends in it, and no more.
    """
    steps: list[Step] = []
    seen = {key(start)}
    queue: list[Point] = [start]
    truncated = False
    withheld: set[tuple[Any, ...]] = set()

    def cut(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal truncated
        truncated = truncated or len(rows) >= ROWS
        return rows

    def add(step: Step, expand: bool) -> None:
        nonlocal truncated
        if readable is not None and step["to"]["project"] not in readable:
            step["to"] = {"project": step["to"]["project"], "id": ""}
            same = (key(step["from"]), key(step["to"]), step["way"], step["relation"])
            if (*same, step["kind"], step["name"]) in withheld:
                return
            withheld.add((*same, step["kind"], step["name"]))
            expand = False
        if len(steps) >= max_steps:
            truncated = True
            return
        steps.append(step)
        if key(step["to"]) not in seen:
            seen.add(key(step["to"]))
            if expand:
                queue.append(step["to"])

    while queue and not truncated:
        point = queue.pop(0)
        ids = around(point["id"])
        directory = point["id"] if point["id"].endswith("/") else None

        exports = cut(
            db.query(
                """SELECT kind, name, node_id FROM provided_names
        WHERE project = $1
          AND (node_id = ANY ($2::text[])
               OR ($3::text IS NOT NULL AND starts_with(node_id, $3)))
        ORDER BY node_id, kind, name
        LIMIT $4""",
                [point["project"], ids, directory, ROWS],
            )
        )
        taken: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        if exports:
            holders = sorted({row["node_id"] for row in exports})
            for taker in cut(
                db.query(
                    """SELECT source_project AS project, source_id AS id,
                relation_type, kind, name, origin, target_id
           FROM project_links
          WHERE target_project = $1 AND target_id = ANY ($2::text[])
          ORDER BY source_project, source_id
          LIMIT $3""",
                    [point["project"], holders, ROWS],
                )
            ):
                what = (taker["target_id"], taker["kind"], taker["name"])
                taken.setdefault(what, []).append(taker)
        for row in exports:
            if truncated and len(steps) >= max_steps:
                break
            holder = {"project": point["project"], "id": row["node_id"]}
            if row["node_id"] != point["id"] and key(holder) not in seen:
                add(
                    {
                        "from": point,
                        "to": holder,
                        "way": "contains",
                        "relation": "provides",
                        "kind": row["kind"],
                        "name": row["name"],
                        "origin": "export",
                    },
                    False,
                )
            for taker in taken.get((row["node_id"], row["kind"], row["name"]), []):
                add(
                    {
                        "from": holder,
                        "to": {"project": taker["project"], "id": taker["id"]},
                        "way": "taken_by",
                        "relation": taker["relation_type"],
                        "kind": taker["kind"],
                        "name": taker["name"],
                        "origin": taker["origin"],
                    },
                    True,
                )

        declared = cut(
            db.query(
                """SELECT source_project AS project, source_id AS id, relation_type,
              kind, name, origin
         FROM project_links
        WHERE target_project = $1 AND target_id = ANY ($2::text[])
          AND origin = 'declared'
        ORDER BY source_project, source_id
        LIMIT $3""",
                [point["project"], ids, ROWS],
            )
        )
        for row in declared:
            add(
                {
                    "from": point,
                    "to": {"project": row["project"], "id": row["id"]},
                    "way": "taken_by",
                    "relation": row["relation_type"],
                    "kind": None,
                    "name": None,
                    "origin": row["origin"],
                },
                True,
            )

        # A file is applied through the classes it defines: an include reaches
        # the class node inside the file, never the file itself.
        file = point["id"].split("::", 1)[0]
        applying = cut(
            db.query(
                """SELECT DISTINCT source_id AS id, relation_type FROM edges
        WHERE project = $1
          AND (target_id = ANY ($2::text[]) OR starts_with(target_id, $4))
          AND source_id <> ALL ($2::text[]) AND NOT starts_with(source_id, $4)
          AND relation_type = ANY ($3::text[])
        ORDER BY relation_type, source_id
        LIMIT $5""",
                [point["project"], [point["id"], file], APPLIED_BY, f"{file}::", ROWS],
            )
        )
        for row in applying:
            add(
                {
                    "from": point,
                    "to": {"project": point["project"], "id": row["id"]},
                    "way": "applied_by",
                    "relation": row["relation_type"],
                    "kind": None,
                    "name": None,
                    "origin": "edge",
                },
                True,
            )

        used = cut(
            db.query(
                """SELECT target_project AS project, target_id AS id, relation_type,
              kind, name, origin
         FROM project_links
        WHERE source_project = $1 AND source_id = ANY ($2::text[])
        ORDER BY target_project, target_id
        LIMIT $3""",
                [point["project"], [point["id"], file], ROWS],
            )
        )
        for row in used:
            add(
                {
                    "from": point,
                    "to": {"project": row["project"], "id": row["id"]},
                    "way": "uses",
                    "relation": row["relation_type"],
                    "kind": row["kind"],
                    "name": row["name"],
                    "origin": row["origin"],
                },
                False,
            )

    return {
        "start": start,
        "steps": steps,
        "chains": trace_chains(start, steps),
        "truncated": truncated,
    }
