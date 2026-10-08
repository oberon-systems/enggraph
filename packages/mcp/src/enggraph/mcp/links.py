"""Links between projects: names one provides and another takes."""

from __future__ import annotations

import re
from typing import Any

from enggraph.mcp import db

DEFAULT_LINK_DEPTH = 1
MAX_LINK_DEPTH = 3
SAMPLES_PER_EDGE = 5
NAMES_PER_KIND = 10
MAX_EDGES = 200
MAX_ROWS = 200
ROOT_ID = "./"

LINK_KINDS = [
    "image",
    "role",
    "npm",
    "composer",
    "pypi",
    "go",
    "cargo",
    "cmake",
    "vcpkg",
    "deploy-role",
    "deploy-module",
    "host",
    "tfmodule",
    "package",
    "bucket",
]
LINK_DIRECTIONS = ["both", "outgoing", "incoming"]

IMAGE_REGISTRY_DEFAULTS = ["docker.io/", "library/"]
PEP508_NAME = re.compile(r"\A\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
SOURCE_GETTER = re.compile(r"\A[a-z0-9]+::")
SOURCE_SCHEME = re.compile(r"\A[a-z0-9+]+://")
SOURCE_USER = re.compile(r"\A[^@/]+@")
NAME_LENGTH = 255
TRAILING_SLASHES = re.compile(r"/+\Z")


def normalize_name(kind: str, raw: str) -> str:
    """Return the name two projects are matched on; the indexer's rule, in step."""
    name = raw.strip()
    if name == "" or "$" in name:
        return ""
    if kind == "image":
        name = name.split("@", 1)[0]
        slash = name.rfind("/")
        head = "" if slash < 0 else name[:slash]
        last = name[slash + 1 :].split(":", 1)[0]
        name = last if head == "" else f"{head}/{last}"
        for prefix in IMAGE_REGISTRY_DEFAULTS:
            name = name.removeprefix(prefix)
    elif kind == "pypi":
        match = PEP508_NAME.match(name)
        name = "" if match is None else re.sub(r"[-_.]+", "-", match.group(1)).lower()
    elif kind == "role":
        name = TRAILING_SLASHES.sub("", name).split("/")[-1]
    elif kind == "host":
        name = re.sub(r"\.+\Z", "", name).lower()
    elif kind == "tfmodule":
        name = module_source(name)
    elif kind == "bucket":
        name = name.lower()
    elif kind == "package" and "%" in name:
        return ""
    return name[:NAME_LENGTH]


def module_source(raw: str) -> str:
    """Return a remote module source without getter, scheme, user or ref."""
    name = SOURCE_GETTER.sub("", raw.lower()).split("?", 1)[0]
    name = SOURCE_USER.sub("", SOURCE_SCHEME.sub("", name))
    colon = name.find(":")
    if colon >= 0:
        host = name[:colon]
        rest = name[colon + 1 :]
        # git@example.com:alpha/infra.git is example.com/alpha/infra.
        if "/" not in host and not re.match(r"[0-9]", rest):
            name = f"{host}/{rest}"
    name = re.sub(r"\.git\Z", "", name.replace(".git//", "//"))
    return TRAILING_SLASHES.sub("", name)


def ancestor_ids(node_id: str) -> list[str]:
    """Return every directory above a node, nearest first, up to the root."""
    path = TRAILING_SLASHES.sub("", node_id.split("::", 1)[0])
    parts = [part for part in path.split("/") if part not in ("", ".")]
    ancestors = [f"{'/'.join(parts[:end])}/" for end in range(len(parts) - 1, 0, -1)]
    if node_id != ROOT_ID:
        ancestors.append(ROOT_ID)
    return ancestors


def edges_along_walk(
    edges: list[db.Row], hops: dict[str, int], direction: str
) -> list[db.Row]:
    """Keep the edges one step apart in the walk, the way it went."""
    kept = []
    for edge in edges:
        start = hops.get(edge["from"])
        end = hops.get(edge["to"])
        if start is None or end is None:
            continue
        # Two starts are two members of one organization: their link is inside it.
        if start == 0 and end == 0:
            near = True
        elif direction == "outgoing":
            near = end == start + 1
        elif direction == "incoming":
            near = start == end + 1
        else:
            near = abs(start - end) == 1
        if near:
            kept.append(edge)
    return kept


def project_links(
    projects: list[str],
    direction: str,
    depth: int,
    relation: str | None,
    node_id: str | None,
) -> dict[str, Any]:
    """Return the projects around the ones named, and the links between them."""
    depth = depth if node_id is None else 1
    reach = db.query(
        """WITH RECURSIVE pairs AS (
       SELECT DISTINCT l.source_project, l.target_project
         FROM project_links AS l
        WHERE ($3::text IS NULL OR l.relation_type = $3)
          AND ($4::text IS NULL
               OR (l.source_project = ANY ($1::text[]) AND l.source_id = $4)
               OR (l.target_project = ANY ($1::text[]) AND l.target_id = $4))
     ), walk (project, hop) AS (
       SELECT start, 0 FROM unnest($1::text[]) AS start
       UNION
       SELECT CASE WHEN p.source_project = w.project
                   THEN p.target_project ELSE p.source_project END,
              w.hop + 1
         FROM walk AS w
         JOIN pairs AS p
           ON ($2 <> 'incoming' AND p.source_project = w.project)
           OR ($2 <> 'outgoing' AND p.target_project = w.project)
        WHERE w.hop < $5
     )
     SELECT project, min(hop)::int AS hop FROM walk GROUP BY project""",
        [projects, direction, relation, node_id, depth],
    )
    hops = {row["project"]: row["hop"] for row in reach}
    reached = list(hops)

    grouped = db.query(
        """SELECT l.source_project AS "from", l.target_project AS "to",
            l.relation_type AS relation, l.kind, l.origin,
            count(*)::int AS count,
            (array_agg(json_build_object('source_id', l.source_id,
                                         'target_id', l.target_id,
                                         'name', l.name)
                       ORDER BY l.source_id, l.target_id))[1:$4] AS samples
       FROM project_links AS l
      WHERE l.source_project = ANY ($1::text[])
        AND l.target_project = ANY ($1::text[])
        AND ($2::text IS NULL OR l.relation_type = $2)
        AND ($3::text IS NULL
             OR (l.source_project = ANY ($5::text[]) AND l.source_id = $3)
             OR (l.target_project = ANY ($5::text[]) AND l.target_id = $3))
      GROUP BY l.source_project, l.target_project, l.relation_type, l.kind,
               l.origin
      ORDER BY l.source_project, l.target_project, l.relation_type, l.kind
      LIMIT $6""",
        [reached, relation, node_id, SAMPLES_PER_EDGE, projects, MAX_EDGES],
    )

    provides = db.query(
        """SELECT project, kind, name, node_id, origin
       FROM provided_names
      WHERE project = ANY ($1::text[])
      ORDER BY project, kind, name
      LIMIT $2""",
        [projects, MAX_ROWS],
    )
    unprovided = db.query(
        """SELECT i.project, i.kind, count(DISTINCT i.name)::int AS count,
            (array_agg(DISTINCT i.name ORDER BY i.name))[1:$2] AS names
       FROM taken_names AS i
      WHERE i.project = ANY ($1::text[])
        AND NOT EXISTS (
              SELECT 1 FROM provided_names AS e
               WHERE e.kind = i.kind AND e.name = i.name)
      GROUP BY i.project, i.kind
      ORDER BY i.project, i.kind""",
        [projects, NAMES_PER_KIND],
    )
    ambiguous = db.query(
        """SELECT i.project, i.kind, i.name,
            array_agg(DISTINCT e.project ORDER BY e.project) AS candidates
       FROM taken_names AS i
       JOIN provided_names AS e ON e.kind = i.kind AND e.name = i.name
      WHERE i.project = ANY ($1::text[])
      GROUP BY i.project, i.kind, i.name
     HAVING count(DISTINCT e.project) > 1 AND NOT bool_or(e.project = i.project)
      ORDER BY i.project, i.kind, i.name
      LIMIT $2""",
        [projects, MAX_ROWS],
    )

    return {
        "projects": hops,
        "edges": edges_along_walk(grouped, hops, direction),
        "provides": provides,
        "unprovided": unprovided,
        "ambiguous": ambiguous,
    }


def link_summary(projects: list[str]) -> dict[str, Any]:
    """Return one step around a project, rolled up, for describe_project."""
    rows = db.query(
        """SELECT 'uses' AS way, l.target_project AS project,
            l.relation_type AS relation, count(*)::int AS count
       FROM project_links AS l
      WHERE l.source_project = ANY ($1::text[])
      GROUP BY l.target_project, l.relation_type
     UNION ALL
     SELECT 'used_by', l.source_project, l.relation_type, count(*)::int
       FROM project_links AS l
      WHERE l.target_project = ANY ($1::text[])
      GROUP BY l.source_project, l.relation_type
      ORDER BY 1, 2, 3""",
        [projects],
    )
    provided = db.query(
        """SELECT count(*)::int AS count FROM provided_names
      WHERE project = ANY ($1::text[])""",
        [projects],
    )

    def pick(way: str) -> list[dict[str, Any]]:
        return [
            {
                "project": row["project"],
                "relation": row["relation"],
                "count": row["count"],
            }
            for row in rows
            if row["way"] == way
        ]

    return {
        "uses": pick("uses"),
        "used_by": pick("used_by"),
        "provides": provided[0]["count"] if provided else 0,
    }


def name_key(raw: str) -> str:
    """Return a name as its letters and digits: alpha_web_01 is alpha-web.01."""
    return re.sub(r"[^a-z0-9]+", "", raw.lower())


# The SQL twin of name_key, applied to the whole name, to a host's first label
# and to the last path segment of an image or a module.
NAME_MATCHES = """(
  regexp_replace(lower(name), '[^a-z0-9]+', '', 'g') = $1
  OR regexp_replace(
       lower(CASE WHEN kind = 'host' THEN split_part(name, '.', 1) END),
       '[^a-z0-9]+', '', 'g') = $1
  OR regexp_replace(
       lower(CASE WHEN name LIKE '%/%'
                  THEN regexp_replace(name, '^.*/', '') END),
       '[^a-z0-9]+', '', 'g') = $1
)"""

NAME_SCOPE = """
  project IN (
    SELECT p.name FROM projects AS p
     WHERE ($2::text IS NULL
            OR p.name = $2
            OR EXISTS (
                 SELECT 1 FROM org_members AS m
                  WHERE m.organization = $2 AND m.project = p.name
               ))
       AND ($3::text IS NULL OR p.type = $3)
  )
  AND ($4::text IS NULL OR kind = $4)"""


def find_name(
    raw: str, named: str | None, project_type: str | None, kind: str | None
) -> dict[str, list[db.Row]]:
    """Return who provides and who takes a name, across the projects in scope.

    A host, an image or a package is no node of any graph; it is a name the
    link tables hold on both sides, and this is the way in from it.
    """
    key = name_key(raw)
    if key == "":
        return {"provides": [], "takes": []}
    args = [key, named, project_type, kind, MAX_ROWS]
    provides = db.query(
        f"""SELECT project, kind, name, node_id, NULL::text AS relation, origin
       FROM provided_names
      WHERE {NAME_MATCHES} AND {NAME_SCOPE}
      ORDER BY kind, name, project, node_id
      LIMIT $5""",
        args,
    )
    takes = db.query(
        f"""SELECT project, kind, name, source_id AS node_id,
            relation_type AS relation, NULL::text AS origin
       FROM taken_names
      WHERE {NAME_MATCHES} AND {NAME_SCOPE}
      ORDER BY kind, name, project, source_id
      LIMIT $5""",
        args,
    )
    return {"provides": provides, "takes": takes}


def node_links(projects: list[str], node_id: str) -> list[db.Row]:
    """Return the links leaving or reaching one node of the projects named."""
    return db.query(
        """SELECT source_project, source_id, target_project, target_id,
            relation_type, kind, name, origin, note
       FROM project_links
      WHERE (source_project = ANY ($1::text[]) AND source_id = $2)
         OR (target_project = ANY ($1::text[]) AND target_id = $2)
      ORDER BY source_project, source_id, target_project, target_id
      LIMIT $3""",
        [projects, node_id, MAX_ROWS],
    )


def links_into(targets: list[dict[str, str]]) -> list[db.Row]:
    """Return the links whose target is one of the (project, id) pairs given."""
    if not targets:
        return []
    return db.query(
        """SELECT l.source_project, l.source_id, l.target_project, l.target_id,
            l.relation_type, l.kind, l.name, l.origin, l.note,
            n.name AS node_name, n.type AS node_type, n.file_path
       FROM project_links AS l
       JOIN unnest($1::text[], $2::text[]) AS t (project, id)
         ON t.project = l.target_project AND t.id = l.target_id
       LEFT JOIN nodes AS n
         ON n.project = l.source_project AND n.id = l.source_id
      ORDER BY l.source_project, l.source_id
      LIMIT $3""",
        [[t["project"] for t in targets], [t["id"] for t in targets], MAX_ROWS],
    )


def node_exists(project: str, node_id: str) -> bool:
    """Say whether a project holds a node."""
    rows = db.query(
        "SELECT 1 FROM nodes WHERE project = $1 AND id = $2", [project, node_id]
    )
    return len(rows) > 0


def _key(relation: dict[str, str]) -> list[str]:
    return [
        relation["source_project"],
        relation["source_id"],
        relation["target_project"],
        relation["target_id"],
        relation["relation"],
    ]


def save_relation(relation: dict[str, str], note: str | None) -> None:
    """Write a link between two projects, or the note of one already there."""
    db.query(
        """INSERT INTO declared_links (
       source_project, source_id, target_project, target_id, relation_type,
       note
     )
     VALUES ($1, $2, $3, $4, $5, $6)
     ON CONFLICT ON CONSTRAINT declared_links_once
     DO UPDATE SET note = EXCLUDED.note""",
        [*_key(relation), note],
    )


def drop_relation(relation: dict[str, str]) -> int:
    """Delete a declared link and say how many rows went."""
    return db.run(
        """DELETE FROM declared_links
      WHERE source_project = $1 AND source_id = $2
        AND target_project = $3 AND target_id = $4
        AND relation_type = $5""",
        _key(relation),
    )[1]


def save_export(project: str, kind: str, name: str, node_id: str) -> None:
    """Write a name a project provides, by hand."""
    db.query(
        """INSERT INTO provided_names (project, kind, name, node_id, origin)
     VALUES ($1, $2, $3, $4, 'manual')
     ON CONFLICT (project, kind, name)
     DO UPDATE SET node_id = EXCLUDED.node_id, origin = 'manual'""",
        [project, kind, name, node_id],
    )


def drop_export(project: str, kind: str, name: str) -> str:
    """Drop an export written by hand; one a run found comes back with the next."""
    dropped = db.query(
        """DELETE FROM provided_names
      WHERE project = $1 AND kind = $2 AND name = $3 AND origin = 'manual'
     RETURNING origin""",
        [project, kind, name],
    )
    if dropped:
        return "dropped"
    left = db.query(
        """SELECT 1 FROM provided_names
      WHERE project = $1 AND kind = $2 AND name = $3""",
        [project, kind, name],
    )
    return "auto" if left else "missing"
