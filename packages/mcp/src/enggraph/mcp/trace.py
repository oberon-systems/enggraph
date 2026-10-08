"""Follow one node across projects: what takes it, applies it and uses it."""

from __future__ import annotations

from typing import Any

from enggraph.core import db
from enggraph.mcp.links import ancestor_ids

DEFAULT_TRACE_STEPS = 60
MAX_TRACE_STEPS = 300
MAX_CHAINS = 30

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


def trace_node(start: Point, max_steps: int) -> dict[str, Any]:
    """Follow one node across projects.

    From a node the walk takes what its directory provides to the projects
    that take it, climbs from what took it to whatever applies that, and
    records what those use in turn without spreading further from a used thing.
    """
    steps: list[Step] = []
    seen = {key(start)}
    queue: list[Point] = [start]
    truncated = False

    def add(step: Step, expand: bool) -> None:
        nonlocal truncated
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

        exports = db.query(
            """SELECT kind, name, node_id FROM provided_names
        WHERE project = $1
          AND (node_id = ANY ($2::text[])
               OR ($3::text IS NOT NULL AND starts_with(node_id, $3)))
        ORDER BY node_id, kind, name""",
            [point["project"], ids, directory],
        )
        for row in exports:
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
            takers = db.query(
                """SELECT source_project AS project, source_id AS id, relation_type,
                kind, name, origin
           FROM project_links
          WHERE target_project = $1 AND target_id = $2
            AND kind IS NOT DISTINCT FROM $3 AND name IS NOT DISTINCT FROM $4
          ORDER BY source_project, source_id""",
                [point["project"], row["node_id"], row["kind"], row["name"]],
            )
            for taker in takers:
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

        declared = db.query(
            """SELECT source_project AS project, source_id AS id, relation_type,
              kind, name, origin
         FROM project_links
        WHERE target_project = $1 AND target_id = ANY ($2::text[])
          AND origin = 'declared'""",
            [point["project"], ids],
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
        applying = db.query(
            """SELECT DISTINCT source_id AS id, relation_type FROM edges
        WHERE project = $1
          AND (target_id = ANY ($2::text[]) OR starts_with(target_id, $4))
          AND source_id <> ALL ($2::text[]) AND NOT starts_with(source_id, $4)
          AND relation_type = ANY ($3::text[])
        ORDER BY relation_type, source_id""",
            [point["project"], [point["id"], file], APPLIED_BY, f"{file}::"],
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

        used = db.query(
            """SELECT target_project AS project, target_id AS id, relation_type,
              kind, name, origin
         FROM project_links
        WHERE source_project = $1 AND source_id = ANY ($2::text[])
        ORDER BY target_project, target_id""",
            [point["project"], [point["id"], file]],
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
