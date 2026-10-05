import type pg from "pg";

import { ancestorIds } from "./links.js";

export const DEFAULT_TRACE_STEPS = 60;
export const MAX_TRACE_STEPS = 300;
const MAX_CHAINS = 30;

// Edges from whatever applies, deploys or configures a node to that node. The
// walk climbs them backwards: from a class to the role that includes it, from
// the role to the node that selects it.
export const APPLIED_BY = [
  "includes_class",
  "configures",
  "selects",
  "has_role",
  "includes_module",
  "requires_module",
  "uses_module",
  "uses_role",
  "includes",
] as const;

export interface TracePoint {
  project: string;
  id: string;
}

export type TraceWay = "contains" | "taken_by" | "applied_by" | "uses";

export interface TraceStep {
  from: TracePoint;
  to: TracePoint;
  way: TraceWay;
  relation: string;
  kind: string | null;
  name: string | null;
  origin: string;
}

export interface Trace {
  start: TracePoint;
  steps: TraceStep[];
  chains: string[];
  truncated: boolean;
}

function key(point: TracePoint): string {
  return `${point.project}\u0000${point.id}`;
}

function label(point: TracePoint): string {
  return `${point.project}:${point.id}`;
}

function arrow(step: TraceStep): string {
  const what = step.kind === null ? "" : ` ${step.kind} ${step.name ?? ""}`;
  return ` -[${step.way} ${step.relation}${what}]-> `;
}

/** Every path from the start to a point the walk went no further from. */
export function traceChains(start: TracePoint, steps: TraceStep[]): string[] {
  const parent = new Map<string, TraceStep>();
  const leaving = new Set<string>();
  for (const step of steps) {
    leaving.add(key(step.from));
    if (!parent.has(key(step.to))) {
      parent.set(key(step.to), step);
    }
  }
  const chains: string[] = [];
  for (const step of steps) {
    const end = key(step.to);
    if (leaving.has(end) || parent.get(end) !== step) {
      continue;
    }
    const path: TraceStep[] = [];
    let current: TraceStep | undefined = step;
    const seen = new Set<string>();
    while (current !== undefined && !seen.has(key(current.to))) {
      seen.add(key(current.to));
      path.unshift(current);
      current =
        key(current.from) === key(start)
          ? undefined
          : parent.get(key(current.from));
    }
    chains.push(
      label(start) +
        path.map((one) => `${arrow(one)}${label(one.to)}`).join(""),
    );
    if (chains.length >= MAX_CHAINS) {
      break;
    }
  }
  return chains;
}

interface ExportRow {
  kind: string;
  name: string;
  node_id: string;
}

interface LinkRow {
  project: string;
  id: string;
  relation_type: string;
  kind: string | null;
  name: string | null;
  origin: string;
}

/** The node itself, its file and every directory above it. */
function around(id: string): string[] {
  const file = id.split("::", 1)[0];
  return [...new Set([id, file, ...ancestorIds(id)])];
}

/**
 * Follow one node across projects.
 *
 * From a node the walk takes what its directory provides to the projects that
 * take it, climbs from what took it to whatever applies that (a role, a
 * node), and records what those use in turn - the host a node deploys to and
 * the project creating it - without spreading further from a used thing.
 */
export async function traceNode(
  pool: pg.Pool,
  start: TracePoint,
  maxSteps: number,
): Promise<Trace> {
  const steps: TraceStep[] = [];
  const seen = new Set<string>([key(start)]);
  const queue: TracePoint[] = [start];
  let truncated = false;

  const add = (step: TraceStep, expand: boolean): void => {
    if (steps.length >= maxSteps) {
      truncated = true;
      return;
    }
    steps.push(step);
    if (!seen.has(key(step.to))) {
      seen.add(key(step.to));
      if (expand) {
        queue.push(step.to);
      }
    }
  };

  while (queue.length > 0 && !truncated) {
    const point = queue.shift() as TracePoint;
    const ids = around(point.id);
    const directory = point.id.endsWith("/") ? point.id : null;

    const exports = await pool.query<ExportRow>(
      `SELECT kind, name, node_id FROM project_exports
        WHERE project = $1
          AND (node_id = ANY ($2::text[])
               OR ($3::text IS NOT NULL AND starts_with(node_id, $3)))
        ORDER BY node_id, kind, name`,
      [point.project, ids, directory],
    );
    for (const row of exports.rows) {
      const holder = { project: point.project, id: row.node_id };
      if (row.node_id !== point.id && !seen.has(key(holder))) {
        add(
          {
            from: point,
            to: holder,
            way: "contains",
            relation: "provides",
            kind: row.kind,
            name: row.name,
            origin: "export",
          },
          false,
        );
      }
      const takers = await pool.query<LinkRow>(
        `SELECT source_project AS project, source_id AS id, relation_type,
                kind, name, origin
           FROM project_links
          WHERE target_project = $1 AND target_id = $2
            AND kind IS NOT DISTINCT FROM $3 AND name IS NOT DISTINCT FROM $4
          ORDER BY source_project, source_id`,
        [point.project, row.node_id, row.kind, row.name],
      );
      for (const taker of takers.rows) {
        add(
          {
            from: holder,
            to: { project: taker.project, id: taker.id },
            way: "taken_by",
            relation: taker.relation_type,
            kind: taker.kind,
            name: taker.name,
            origin: taker.origin,
          },
          true,
        );
      }
    }

    const declared = await pool.query<LinkRow>(
      `SELECT source_project AS project, source_id AS id, relation_type,
              kind, name, origin
         FROM project_links
        WHERE target_project = $1 AND target_id = ANY ($2::text[])
          AND origin = 'declared'`,
      [point.project, ids],
    );
    for (const row of declared.rows) {
      add(
        {
          from: point,
          to: { project: row.project, id: row.id },
          way: "taken_by",
          relation: row.relation_type,
          kind: null,
          name: null,
          origin: row.origin,
        },
        true,
      );
    }

    // A file is applied through the classes it defines: `include alpha::install`
    // reaches the class node inside install.pp, never the file itself.
    const file = point.id.split("::", 1)[0];
    const applying = await pool.query<{ id: string; relation_type: string }>(
      `SELECT DISTINCT source_id AS id, relation_type FROM graph_edges
        WHERE project = $1
          AND (target_id = ANY ($2::text[]) OR starts_with(target_id, $4))
          AND source_id <> ALL ($2::text[]) AND NOT starts_with(source_id, $4)
          AND relation_type = ANY ($3::text[])
        ORDER BY relation_type, source_id`,
      [point.project, [point.id, file], [...APPLIED_BY], `${file}::`],
    );
    for (const row of applying.rows) {
      add(
        {
          from: point,
          to: { project: point.project, id: row.id },
          way: "applied_by",
          relation: row.relation_type,
          kind: null,
          name: null,
          origin: "edge",
        },
        true,
      );
    }

    const used = await pool.query<LinkRow>(
      `SELECT target_project AS project, target_id AS id, relation_type,
              kind, name, origin
         FROM project_links
        WHERE source_project = $1 AND source_id = ANY ($2::text[])
        ORDER BY target_project, target_id`,
      [point.project, [point.id, point.id.split("::", 1)[0]]],
    );
    for (const row of used.rows) {
      add(
        {
          from: point,
          to: { project: row.project, id: row.id },
          way: "uses",
          relation: row.relation_type,
          kind: row.kind,
          name: row.name,
          origin: row.origin,
        },
        false,
      );
    }
  }

  return { start, steps, chains: traceChains(start, steps), truncated };
}
