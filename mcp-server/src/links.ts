import type pg from "pg";

export const DEFAULT_LINK_DEPTH = 1;
export const MAX_LINK_DEPTH = 3;
const SAMPLES_PER_EDGE = 5;
const NAMES_PER_KIND = 10;
const MAX_EDGES = 200;
const MAX_ROWS = 200;
const ROOT_ID = "./";

export const LINK_KINDS = [
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
] as const;
export type LinkKind = (typeof LINK_KINDS)[number];

export const LINK_DIRECTIONS = ["both", "outgoing", "incoming"] as const;
export type LinkDirection = (typeof LINK_DIRECTIONS)[number];

const IMAGE_REGISTRY_DEFAULTS = ["docker.io/", "library/"];
const PEP508_NAME = /^\s*([A-Za-z0-9][A-Za-z0-9._-]*)/;
const SOURCE_GETTER = /^[a-z0-9]+::/;
const SOURCE_SCHEME = /^[a-z0-9+]+:\/\//;
const SOURCE_USER = /^[^@/]+@/;
const NAME_LENGTH = 255;

/** The name two projects are matched on; graphify's normalize, kept in step. */
export function normalizeName(kind: string, raw: string): string {
  let name = raw.trim();
  if (name === "" || name.includes("$")) {
    return "";
  }
  if (kind === "image") {
    name = name.split("@", 1)[0];
    const slash = name.lastIndexOf("/");
    const head = slash < 0 ? "" : name.slice(0, slash);
    const last = name.slice(slash + 1).split(":", 1)[0];
    name = head === "" ? last : `${head}/${last}`;
    for (const prefix of IMAGE_REGISTRY_DEFAULTS) {
      if (name.startsWith(prefix)) {
        name = name.slice(prefix.length);
      }
    }
  } else if (kind === "pypi") {
    const match = PEP508_NAME.exec(name);
    name = match === null ? "" : match[1].replace(/[-_.]+/g, "-").toLowerCase();
  } else if (kind === "role") {
    const parts = name.replace(/\/+$/, "").split("/");
    name = parts[parts.length - 1];
  } else if (kind === "host") {
    name = name.replace(/\.+$/, "").toLowerCase();
  } else if (kind === "tfmodule") {
    name = moduleSource(name);
  }
  return name.slice(0, NAME_LENGTH);
}

/** A remote module source without getter, scheme, user or ref. */
function moduleSource(raw: string): string {
  let name = raw.toLowerCase().replace(SOURCE_GETTER, "").split("?", 1)[0];
  name = name.replace(SOURCE_SCHEME, "").replace(SOURCE_USER, "");
  const colon = name.indexOf(":");
  if (colon >= 0) {
    const host = name.slice(0, colon);
    const rest = name.slice(colon + 1);
    // git@example.com:alpha/infra.git is example.com/alpha/infra.
    if (!host.includes("/") && !/^\d/.test(rest)) {
      name = `${host}/${rest}`;
    }
  }
  return name
    .replace(/\.git\/\//g, "//")
    .replace(/\.git$/, "")
    .replace(/\/+$/, "");
}

/** Every directory above a node, nearest first, up to the root. */
export function ancestorIds(id: string): string[] {
  const path = id.split("::", 1)[0].replace(/\/+$/, "");
  const parts = path.split("/").filter((part) => part !== "" && part !== ".");
  const ancestors: string[] = [];
  for (let end = parts.length - 1; end > 0; end -= 1) {
    ancestors.push(`${parts.slice(0, end).join("/")}/`);
  }
  if (id !== ROOT_ID) {
    ancestors.push(ROOT_ID);
  }
  return ancestors;
}

export interface LinkRow {
  source_project: string;
  source_id: string;
  target_project: string;
  target_id: string;
  relation_type: string;
  kind: string | null;
  name: string | null;
  origin: "matched" | "declared";
  note: string | null;
}

export interface ProjectEdge {
  from: string;
  to: string;
  relation: string;
  kind: string | null;
  origin: string;
  count: number;
  samples: { source_id: string; target_id: string; name: string | null }[];
}

export interface ProjectLinks {
  projects: Record<string, number>;
  edges: ProjectEdge[];
  provides: {
    project: string;
    kind: string;
    name: string;
    node_id: string;
    origin: string;
  }[];
  unprovided: {
    project: string;
    kind: string;
    count: number;
    names: string[];
  }[];
  ambiguous: {
    project: string;
    kind: string;
    name: string;
    candidates: string[];
  }[];
}

export interface LinkQuery {
  projects: string[];
  direction: LinkDirection;
  depth: number;
  relation: string | null;
  nodeId: string | null;
}

/** Keep the edges one step apart in the walk, the way it went. */
export function edgesAlongWalk(
  edges: ProjectEdge[],
  hops: Record<string, number>,
  direction: LinkDirection,
): ProjectEdge[] {
  return edges.filter((edge) => {
    const from = hops[edge.from];
    const to = hops[edge.to];
    if (from === undefined || to === undefined) {
      return false;
    }
    // Two starts are two members of one organization: their link is inside it.
    if (from === 0 && to === 0) {
      return true;
    }
    if (direction === "outgoing") {
      return to === from + 1;
    }
    if (direction === "incoming") {
      return from === to + 1;
    }
    return Math.abs(from - to) === 1;
  });
}

export async function projectLinks(
  pool: pg.Pool,
  query: LinkQuery,
): Promise<ProjectLinks> {
  const depth = query.nodeId === null ? query.depth : 1;
  const reach = await pool.query<{ project: string; hop: number }>(
    `WITH RECURSIVE pairs AS (
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
     SELECT project, min(hop)::int AS hop FROM walk GROUP BY project`,
    [query.projects, query.direction, query.relation, query.nodeId, depth],
  );
  const hops: Record<string, number> = {};
  for (const row of reach.rows) {
    hops[row.project] = row.hop;
  }
  const reached = Object.keys(hops);

  const grouped = await pool.query<ProjectEdge>(
    `SELECT l.source_project AS "from", l.target_project AS "to",
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
      LIMIT $6`,
    [
      reached,
      query.relation,
      query.nodeId,
      SAMPLES_PER_EDGE,
      query.projects,
      MAX_EDGES,
    ],
  );

  const provides = await pool.query<ProjectLinks["provides"][number]>(
    `SELECT project, kind, name, node_id, origin
       FROM project_exports
      WHERE project = ANY ($1::text[])
      ORDER BY project, kind, name
      LIMIT $2`,
    [query.projects, MAX_ROWS],
  );
  const unprovided = await pool.query<ProjectLinks["unprovided"][number]>(
    `SELECT i.project, i.kind, count(DISTINCT i.name)::int AS count,
            (array_agg(DISTINCT i.name ORDER BY i.name))[1:$2] AS names
       FROM project_imports AS i
      WHERE i.project = ANY ($1::text[])
        AND NOT EXISTS (
              SELECT 1 FROM project_exports AS e
               WHERE e.kind = i.kind AND e.name = i.name)
      GROUP BY i.project, i.kind
      ORDER BY i.project, i.kind`,
    [query.projects, NAMES_PER_KIND],
  );
  const ambiguous = await pool.query<ProjectLinks["ambiguous"][number]>(
    `SELECT i.project, i.kind, i.name,
            array_agg(DISTINCT e.project ORDER BY e.project) AS candidates
       FROM project_imports AS i
       JOIN project_exports AS e ON e.kind = i.kind AND e.name = i.name
      WHERE i.project = ANY ($1::text[])
      GROUP BY i.project, i.kind, i.name
     HAVING count(DISTINCT e.project) > 1 AND NOT bool_or(e.project = i.project)
      ORDER BY i.project, i.kind, i.name
      LIMIT $2`,
    [query.projects, MAX_ROWS],
  );

  return {
    projects: hops,
    edges: edgesAlongWalk(grouped.rows, hops, query.direction),
    provides: provides.rows,
    unprovided: unprovided.rows,
    ambiguous: ambiguous.rows,
  };
}

/** One step around a project, rolled up, for describe_project. */
export async function linkSummary(
  pool: pg.Pool,
  projects: string[],
): Promise<{
  uses: { project: string; relation: string; count: number }[];
  used_by: { project: string; relation: string; count: number }[];
  provides: number;
}> {
  const res = await pool.query<{
    way: "uses" | "used_by";
    project: string;
    relation: string;
    count: number;
  }>(
    `SELECT 'uses' AS way, l.target_project AS project,
            l.relation_type AS relation, count(*)::int AS count
       FROM project_links AS l
      WHERE l.source_project = ANY ($1::text[])
      GROUP BY l.target_project, l.relation_type
     UNION ALL
     SELECT 'used_by', l.source_project, l.relation_type, count(*)::int
       FROM project_links AS l
      WHERE l.target_project = ANY ($1::text[])
      GROUP BY l.source_project, l.relation_type
      ORDER BY 1, 2, 3`,
    [projects],
  );
  const provided = await pool.query<{ count: number }>(
    `SELECT count(*)::int AS count FROM project_exports
      WHERE project = ANY ($1::text[])`,
    [projects],
  );
  const pick = (way: "uses" | "used_by") =>
    res.rows
      .filter((row) => row.way === way)
      .map(({ project, relation, count }) => ({ project, relation, count }));
  return {
    uses: pick("uses"),
    used_by: pick("used_by"),
    provides: provided.rows[0]?.count ?? 0,
  };
}

/** The links leaving or reaching one node of the projects named. */
export async function nodeLinks(
  pool: pg.Pool,
  projects: string[],
  nodeId: string,
): Promise<LinkRow[]> {
  const res = await pool.query<LinkRow>(
    `SELECT source_project, source_id, target_project, target_id,
            relation_type, kind, name, origin, note
       FROM project_links
      WHERE (source_project = ANY ($1::text[]) AND source_id = $2)
         OR (target_project = ANY ($1::text[]) AND target_id = $2)
      ORDER BY source_project, source_id, target_project, target_id
      LIMIT $3`,
    [projects, nodeId, MAX_ROWS],
  );
  return res.rows;
}

export interface LinkedNode extends LinkRow {
  node_name: string | null;
  node_type: string | null;
  file_path: string | null;
}

/** The links whose target is one of the (project, id) pairs given. */
export async function linksInto(
  pool: pg.Pool,
  targets: { project: string; id: string }[],
): Promise<LinkedNode[]> {
  if (targets.length === 0) {
    return [];
  }
  const res = await pool.query<LinkedNode>(
    `SELECT l.source_project, l.source_id, l.target_project, l.target_id,
            l.relation_type, l.kind, l.name, l.origin, l.note,
            n.name AS node_name, n.type AS node_type, n.file_path
       FROM project_links AS l
       JOIN unnest($1::text[], $2::text[]) AS t (project, id)
         ON t.project = l.target_project AND t.id = l.target_id
       LEFT JOIN graph_nodes AS n
         ON n.project = l.source_project AND n.id = l.source_id
      ORDER BY l.source_project, l.source_id
      LIMIT $3`,
    [targets.map((t) => t.project), targets.map((t) => t.id), MAX_ROWS],
  );
  return res.rows;
}

export async function nodeExists(
  pool: pg.Pool,
  project: string,
  nodeId: string,
): Promise<boolean> {
  const res = await pool.query(
    `SELECT 1 FROM graph_nodes WHERE project = $1 AND id = $2`,
    [project, nodeId],
  );
  return (res.rowCount ?? 0) > 0;
}

export interface Relation {
  sourceProject: string;
  sourceId: string;
  targetProject: string;
  targetId: string;
  relation: string;
}

export async function saveRelation(
  pool: pg.Pool,
  relation: Relation,
  note: string | null,
): Promise<void> {
  await pool.query(
    `INSERT INTO project_relations (
       source_project, source_id, target_project, target_id, relation_type,
       note
     )
     VALUES ($1, $2, $3, $4, $5, $6)
     ON CONFLICT ON CONSTRAINT project_relations_once
     DO UPDATE SET note = EXCLUDED.note`,
    [
      relation.sourceProject,
      relation.sourceId,
      relation.targetProject,
      relation.targetId,
      relation.relation,
      note,
    ],
  );
}

export async function dropRelation(
  pool: pg.Pool,
  relation: Relation,
): Promise<number> {
  const res = await pool.query(
    `DELETE FROM project_relations
      WHERE source_project = $1 AND source_id = $2
        AND target_project = $3 AND target_id = $4
        AND relation_type = $5`,
    [
      relation.sourceProject,
      relation.sourceId,
      relation.targetProject,
      relation.targetId,
      relation.relation,
    ],
  );
  return res.rowCount ?? 0;
}

export async function saveExport(
  pool: pg.Pool,
  project: string,
  kind: string,
  name: string,
  nodeId: string,
): Promise<void> {
  await pool.query(
    `INSERT INTO project_exports (project, kind, name, node_id, origin)
     VALUES ($1, $2, $3, $4, 'manual')
     ON CONFLICT (project, kind, name)
     DO UPDATE SET node_id = EXCLUDED.node_id, origin = 'manual'`,
    [project, kind, name, nodeId],
  );
}

/** Drop an export written by hand; one a run found comes back with the next. */
export async function dropExport(
  pool: pg.Pool,
  project: string,
  kind: string,
  name: string,
): Promise<"dropped" | "auto" | "missing"> {
  const res = await pool.query<{ origin: string }>(
    `DELETE FROM project_exports
      WHERE project = $1 AND kind = $2 AND name = $3 AND origin = 'manual'
     RETURNING origin`,
    [project, kind, name],
  );
  if ((res.rowCount ?? 0) > 0) {
    return "dropped";
  }
  const left = await pool.query(
    `SELECT 1 FROM project_exports
      WHERE project = $1 AND kind = $2 AND name = $3`,
    [project, kind, name],
  );
  return (left.rowCount ?? 0) > 0 ? "auto" : "missing";
}
