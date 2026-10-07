import type pg from "pg";

import { ancestorIds } from "./links.js";

export const DEFAULT_RELATION = "about";
const MAX_NODES = 50;
const MAX_KNOWLEDGE = 30;
const MAX_TOP = 5;

export const SUGGESTION_GROUPS = [
  "kind",
  "lever",
  "about",
  "directory",
] as const;
export type SuggestionGroup = (typeof SUGGESTION_GROUPS)[number];

export interface NodeRef {
  project: string;
  node_id: string;
}

export interface RecordNode extends NodeRef {
  relation: string;
  missing: boolean;
}

export interface Knowledge {
  record_project: string;
  record_id: string;
  record_type: string;
  title: string;
  summary: string | null;
  status: string | null;
  about: string | null;
  project: string;
  node_id: string;
  /** The node asked about; differs from node_id for a directory above it. */
  via: string;
  relation: string;
}

function readRef(
  item: unknown,
  fallback: string | null,
): { project: string | null; node_id: unknown } | null {
  if (typeof item === "string") {
    return { project: fallback, node_id: item };
  }
  if (typeof item !== "object" || item === null) {
    return null;
  }
  const { project, node_id } = item as { project?: unknown; node_id?: unknown };
  const named = typeof project === "string" ? project.trim() : "";
  return { project: named || fallback, node_id };
}

/**
 * Read a `nodes` argument: node ids in the default project, or objects naming
 * their project. Absent is null, which keeps a record's links as they are.
 */
export function parseNodeRefs(
  raw: unknown,
  fallback: string | null,
): NodeRef[] | null {
  if (raw === undefined || raw === null) {
    return null;
  }
  if (!Array.isArray(raw)) {
    throw new Error('Argument "nodes" must be an array');
  }
  const refs = new Map<string, NodeRef>();
  for (const item of raw) {
    const ref = readRef(item, fallback);
    if (
      ref === null ||
      typeof ref.node_id !== "string" ||
      ref.node_id.trim() === ""
    ) {
      throw new Error(
        'Every entry of "nodes" is a node id or { project, node_id }',
      );
    }
    if (ref.project === null || ref.project === "") {
      throw new Error(
        `Node ${ref.node_id} names no project, and the record has none to ` +
          "lend it: pass { project, node_id }",
      );
    }
    const node = { project: ref.project, node_id: ref.node_id.trim() };
    refs.set(`${node.project}\u0000${node.node_id}`, node);
  }
  if (refs.size > MAX_NODES) {
    throw new Error(`A record names at most ${MAX_NODES} nodes`);
  }
  return [...refs.values()];
}

/** Refuse nodes that do not exist, naming every one of them. */
export async function requireNodes(
  pool: pg.Pool,
  refs: NodeRef[],
): Promise<void> {
  if (refs.length === 0) {
    return;
  }
  const res = await pool.query<NodeRef>(
    `SELECT w.project, w.node_id
       FROM unnest($1::text[], $2::text[]) AS w (project, node_id)
      WHERE NOT EXISTS (
              SELECT 1 FROM nodes AS n
               WHERE n.project = w.project AND n.id = w.node_id
            )`,
    [refs.map((ref) => ref.project), refs.map((ref) => ref.node_id)],
  );
  if (res.rows.length > 0) {
    const missing = res.rows.map((row) => `${row.project}:${row.node_id}`);
    throw new Error(
      `No such node: ${missing.join(", ")}. Find the id with ` +
        "search_code_nodes; nothing was saved",
    );
  }
}

/** Make a record's links exactly `refs`, inside the caller's transaction. */
export async function replaceRecordNodes(
  client: pg.PoolClient,
  recordProject: string,
  recordId: string,
  refs: NodeRef[],
): Promise<void> {
  await client.query(
    `DELETE FROM record_links WHERE record_project = $1 AND record_id = $2`,
    [recordProject, recordId],
  );
  await client.query(
    `INSERT INTO record_links (record_project, record_id, project, node_id)
     SELECT $1::text, $2::text, w.project, w.node_id
       FROM unnest($3::text[], $4::text[]) AS w (project, node_id)`,
    [
      recordProject,
      recordId,
      refs.map((ref) => ref.project),
      refs.map((ref) => ref.node_id),
    ],
  );
}

/** The links of each record, a node that is gone marked missing. */
export async function nodesOfRecords(
  pool: pg.Pool,
  recordProject: string,
  recordIds: string[],
): Promise<Map<string, RecordNode[]>> {
  const found = new Map<string, RecordNode[]>();
  if (recordIds.length === 0) {
    return found;
  }
  const res = await pool.query<RecordNode & { record_id: string }>(
    `SELECT r.record_id, r.project, r.node_id, r.relation,
            NOT EXISTS (
              SELECT 1 FROM nodes AS n
               WHERE n.project = r.project AND n.id = r.node_id
            ) AS missing
       FROM record_links AS r
      WHERE r.record_project = $1 AND r.record_id = ANY ($2::text[])
      ORDER BY r.record_id, r.project, r.node_id`,
    [recordProject, recordIds],
  );
  for (const { record_id, ...node } of res.rows) {
    found.set(record_id, [...(found.get(record_id) ?? []), node]);
  }
  return found;
}

/** Add `nodes` to every row of a record listing. */
export async function withNodes<T extends { id: string }>(
  pool: pg.Pool,
  recordProject: string,
  rows: T[],
): Promise<(T & { nodes: RecordNode[] })[]> {
  const nodes = await nodesOfRecords(
    pool,
    recordProject,
    rows.map((row) => row.id),
  );
  return rows.map((row) => ({ ...row, nodes: nodes.get(row.id) ?? [] }));
}

interface Around {
  project: string[];
  id: string[];
  via: string[];
}

/** The node itself and every directory above it, each pointing back at it. */
function around(refs: NodeRef[]): Around {
  const project: string[] = [];
  const id: string[] = [];
  const via: string[] = [];
  for (const ref of refs) {
    for (const one of [ref.node_id, ...ancestorIds(ref.node_id)]) {
      project.push(ref.project);
      id.push(one);
      via.push(ref.node_id);
    }
  }
  return { project, id, via };
}

/** What the records say about nodes or the directories above them. */
export async function knowledgeFor(
  pool: pg.Pool,
  refs: NodeRef[],
  limit: number = MAX_KNOWLEDGE,
): Promise<Knowledge[]> {
  if (refs.length === 0) {
    return [];
  }
  const wanted = around(refs);
  const res = await pool.query<Knowledge>(
    `SELECT DISTINCT ON (r.record_project, r.record_id)
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
      LIMIT $4`,
    [wanted.project, wanted.id, wanted.via, limit],
  );
  return res.rows;
}

/** The records of one kind attached to a node or a directory above it. */
export async function recordsAbout(
  pool: pg.Pool,
  recordProject: string,
  ref: NodeRef,
): Promise<string[]> {
  const wanted = around([ref]);
  const res = await pool.query<{ record_id: string }>(
    `SELECT DISTINCT r.record_id
       FROM record_links AS r
       JOIN unnest($2::text[], $3::text[]) AS w (project, id)
         ON r.project = w.project AND r.node_id = w.id
      WHERE r.record_project = $1`,
    [recordProject, wanted.project, wanted.id],
  );
  return res.rows.map((row) => row.record_id);
}

/** Narrow an id filter to the records on a node; null keeps it open. */
export function narrowIds(
  ids: string[] | null,
  attached: string[] | null,
): string[] | null {
  if (attached === null) {
    return ids;
  }
  return ids === null ? attached : ids.filter((id) => attached.includes(id));
}

export interface GapGroup {
  key: string;
  project: string | null;
  records: number;
  hits: number;
  top: { id: string; title: string; hits: number }[];
}

export interface GapScope {
  about: string[] | null;
  status: string | null;
  kind: string | null;
}

/** Open gaps rolled up by a field, or by the directories their nodes sit in. */
export async function groupSuggestions(
  pool: pg.Pool,
  recordProject: string,
  by: SuggestionGroup,
  scope: GapScope,
): Promise<GapGroup[]> {
  const args = [recordProject, scope.about, scope.status, scope.kind, MAX_TOP];
  if (by === "directory") {
    const res = await pool.query<GapGroup>(
      `WITH gaps AS (
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
        ORDER BY hits DESC, records DESC, project, directory`,
      args,
    );
    return res.rows;
  }
  const res = await pool.query<GapGroup>(
    `SELECT COALESCE(metadata ->> $6, '(none)') AS key,
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
      ORDER BY hits DESC, records DESC, key`,
    [...args, by],
  );
  return res.rows;
}

/** How many records of each type are attached to nodes of these projects. */
export async function knowledgeCounts(
  pool: pg.Pool,
  projects: string[],
): Promise<Record<string, number>> {
  const res = await pool.query<{ type: string; count: number }>(
    `SELECT n.type,
            count(DISTINCT (r.record_project, r.record_id))::int AS count
       FROM record_links AS r
       JOIN nodes AS n
         ON n.project = r.record_project AND n.id = r.record_id
      WHERE r.project = ANY ($1::text[])
      GROUP BY n.type
      ORDER BY n.type`,
    [projects],
  );
  return Object.fromEntries(res.rows.map((row) => [row.type, row.count]));
}
