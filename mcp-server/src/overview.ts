import type pg from "pg";
import { estimateTokens } from "./context.js";

export const ROOT_ID = "./";
export const DEFAULT_OVERVIEW_DEPTH = 2;
export const MAX_OVERVIEW_DEPTH = 6;
export const DEFAULT_OVERVIEW_BUDGET = 4000;
// A tree deeper or wider than this is cut by the budget long before it ends.
const MAX_OVERVIEW_ROWS = 3000;

export interface OverviewRequest {
  projects: string[];
  path: string;
  depth: number;
  includeEntities: boolean;
  tokenBudget: number;
}

export interface OverviewItem {
  id: string;
  name: string;
  type: string;
  summary: string | null;
  summary_source: string | null;
  children: number;
  items?: OverviewItem[];
}

export interface OverviewTree {
  project: string;
  root: OverviewItem;
}

export interface Overview {
  path: string;
  depth: number;
  budget: { limit: number; used: number; truncated: boolean };
  trees: OverviewTree[];
  notes: string[];
}

interface OverviewRow {
  project: string;
  id: string;
  parent: string | null;
  depth: number;
  name: string;
  type: string;
  summary: string | null;
  summary_source: string | null;
  children: number;
}

/** Read "", "." and "/" as the root, and a directory with or without its slash. */
export function candidateIds(path: string): string[] {
  const trimmed = path.trim().replace(/^\.\/(?=.)/, "");
  if (
    trimmed === "" ||
    trimmed === "." ||
    trimmed === "/" ||
    trimmed === ROOT_ID
  ) {
    return [ROOT_ID];
  }
  return trimmed.endsWith("/")
    ? [trimmed, trimmed.slice(0, -1)]
    : [trimmed, `${trimmed}/`];
}

/** Keep rows breadth first until the budget is spent, and nest what was kept. */
export function shape(
  rows: OverviewRow[],
  budget: number,
): { trees: OverviewTree[]; used: number; truncated: boolean } {
  const ordered = [...rows].sort(
    (a, b) =>
      a.depth - b.depth ||
      a.project.localeCompare(b.project) ||
      Number(a.type !== "directory") - Number(b.type !== "directory") ||
      a.id.localeCompare(b.id),
  );
  const items = new Map<string, OverviewItem>();
  const trees: OverviewTree[] = [];
  let used = 0;
  let truncated = false;
  for (const row of ordered) {
    const key = `${row.project}\u0000${row.id}`;
    const parent =
      row.parent === null
        ? undefined
        : items.get(`${row.project}\u0000${row.parent}`);
    if (row.parent !== null && parent === undefined) {
      truncated = true;
      continue;
    }
    const item: OverviewItem = {
      id: row.id,
      name: row.name,
      type: row.type,
      summary: row.summary,
      summary_source: row.summary_source,
      children: row.children,
    };
    const price = estimateTokens(JSON.stringify(item));
    if (row.parent !== null && used + price > budget) {
      truncated = true;
      continue;
    }
    used += price;
    items.set(key, item);
    if (parent === undefined) {
      trees.push({ project: row.project, root: item });
    } else {
      (parent.items ??= []).push(item);
    }
  }
  return { trees, used, truncated };
}

/** Walk `contains` down from a directory or file node, summaries only. */
export async function buildOverview(
  pool: pg.Pool,
  ask: OverviewRequest,
): Promise<Overview> {
  const res = await pool.query<OverviewRow>(
    `WITH RECURSIVE start AS (
       SELECT DISTINCT ON (n.project) n.project, n.id
         FROM graph_nodes AS n
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
         JOIN graph_edges AS e
           ON e.project = d.project AND e.source_id = d.id
          AND e.relation_type = 'contains'
         JOIN graph_nodes AS c ON c.project = e.project AND c.id = e.target_id
        WHERE d.depth < $3
          AND ($4 OR c.type IN ('directory', 'file'))
     )
     SELECT d.project, d.id, d.parent, d.depth, n.name, n.type, n.summary,
            n.metadata ->> 'summary_source' AS summary_source,
            (SELECT COUNT(*)::int
               FROM graph_edges AS e
              WHERE e.project = d.project AND e.source_id = d.id
                AND e.relation_type = 'contains') AS children
       FROM down AS d
       JOIN graph_nodes AS n ON n.project = d.project AND n.id = d.id
      ORDER BY d.depth, d.project, d.id
      LIMIT $5`,
    [
      ask.projects,
      candidateIds(ask.path),
      ask.depth,
      ask.includeEntities,
      MAX_OVERVIEW_ROWS,
    ],
  );
  const { trees, used, truncated } = shape(res.rows, ask.tokenBudget);
  const notes: string[] = [];
  if (trees.length === 0) {
    notes.push(
      `No node ${ask.path || ROOT_ID} in scope. Directory ids end in a ` +
        'slash ("src/"), the repository is "./"; a project indexed before ' +
        "directories existed gets them on its next index run.",
    );
  }
  if (truncated) {
    notes.push(
      "Budget spent: deeper items were left out. Drill down by calling " +
        "get_overview on a listed directory, or raise token_budget.",
    );
  }
  return {
    path: ask.path || ROOT_ID,
    depth: ask.depth,
    budget: { limit: ask.tokenBudget, used, truncated },
    trees,
    notes,
  };
}
