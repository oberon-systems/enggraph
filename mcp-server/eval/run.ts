import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { performance } from "node:perf_hooks";
import pg from "pg";
import { parse } from "yaml";
import {
  buildContext,
  DEFAULT_EXPAND,
  DEFAULT_TOKEN_BUDGET,
} from "../src/context.js";
import type { ContextEntry } from "../src/context.js";
import { rerank } from "../src/rerank.js";
import { hybridSearch } from "../src/search.js";
import { findSymbol, impactAnalysis } from "../src/symbols.js";

const EVAL_DIR = resolve(import.meta.dirname, "../../eval");
const SEARCH_LIMIT = 10;
const QUALITY = [
  "recall_at_5",
  "recall_at_10",
  "mrr",
  "context_recall",
  "context_precision",
] as const;

type Quality = (typeof QUALITY)[number];

interface Query {
  id: string;
  kind: string;
  query: string;
  expect: string[];
  symbol?: string;
}

// The kinds a symbol tool answers, scored through that tool as well as search.
const KIND_TOOL: Record<string, "find_callers" | "find_tests" | "impact"> = {
  callers: "find_callers",
  tests: "find_tests",
  impact: "impact",
};

interface ToolScored {
  id: string;
  tool: string;
  recall_at_5: number;
  recall: number;
  mrr: number;
  missed: string[];
}

interface Scored extends Record<Quality, number> {
  id: string;
  kind: string;
  tokens: number;
  search_ms: number;
  context_ms: number;
  missed: string[];
  noise: Record<string, number>;
}

interface Summary extends Record<Quality, number> {
  queries: number;
  tokens_mean: number;
  search_ms_p50: number;
  search_ms_p95: number;
  context_ms_p50: number;
  context_ms_p95: number;
}

interface Options {
  rerank: boolean;
  updateBaseline: boolean;
  tolerance: number;
}

function options(argv: string[]): Options {
  const flag = (name: string) =>
    argv.find((arg) => arg.startsWith(`--${name}`));
  const tolerance = flag("tolerance")?.split("=")[1];
  return {
    rerank: flag("rerank") !== "--rerank=false",
    updateBaseline: flag("update-baseline") !== undefined,
    tolerance: tolerance === undefined ? 0.02 : Number(tolerance),
  };
}

function percentile(values: number[], p: number): number {
  const sorted = [...values].sort((a, b) => a - b);
  return (
    sorted[
      Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))
    ] ?? 0
  );
}

function mean(values: number[]): number {
  return values.length === 0
    ? 0
    : values.reduce((a, b) => a + b, 0) / values.length;
}

function round(value: number): number {
  return Number(value.toFixed(4));
}

function recall(expect: string[], found: string[]): number {
  return expect.filter((path) => found.includes(path)).length / expect.length;
}

// A directory hit is scored by its id: it has no file path of its own.
function place(node: { id: string; type: string; file_path: string | null }) {
  return node.file_path ?? (node.type === "directory" ? node.id : null);
}

async function score(
  pool: pg.Pool,
  project: string,
  q: Query,
  opts: Options,
): Promise<{ scored: Scored; semantic: boolean }> {
  let started = performance.now();
  const found = await hybridSearch(pool, {
    named: project,
    kind: null,
    query: q.query,
    limit: SEARCH_LIMIT,
    directories: q.kind === "overview",
  });
  const ranked = rerank(found.rows, q.query, SEARCH_LIMIT, opts.rerank);
  const searchMs = performance.now() - started;
  const files = [
    ...new Set(
      ranked
        .map(({ row }) => place(row))
        .filter((path): path is string => path !== null),
    ),
  ];
  const first = files.findIndex((path) => q.expect.includes(path));

  started = performance.now();
  const packet = await buildContext(pool, {
    query: q.query,
    named: project,
    kind: null,
    seeds: 8,
    tokenBudget: DEFAULT_TOKEN_BUDGET,
    expand: DEFAULT_EXPAND,
    includeChunks: true,
    rerankEnabled: opts.rerank,
    // The code kinds keep the packet the baselines were recorded against.
    detail: q.kind === "overview" ? "summary" : "source",
  });
  const contextMs = performance.now() - started;
  const inPacket = packet.entries
    .map((entry) => place(entry))
    .filter((path): path is string => path !== null);
  const relevant = packet.entries.filter((entry) => {
    const path = place(entry);
    return path !== null && q.expect.includes(path);
  });

  return {
    semantic:
      found.vectorAvailable &&
      found.rows.some((row) => row.vector_rank !== null),
    scored: {
      id: q.id,
      kind: q.kind,
      recall_at_5: recall(q.expect, files.slice(0, 5)),
      recall_at_10: recall(q.expect, files.slice(0, 10)),
      mrr: first === -1 ? 0 : 1 / (first + 1),
      context_recall: recall(q.expect, inPacket),
      context_precision:
        packet.entries.length === 0
          ? 0
          : relevant.length / packet.entries.length,
      tokens: packet.budget.used,
      search_ms: searchMs,
      context_ms: contextMs,
      missed: q.expect.filter((path) => !inPacket.includes(path)),
      noise: noiseBy(packet.entries, q.expect),
    },
  };
}

// The relation and direction the `why` of an entry opens with ("calls into",
// "imports_from from", "defined in") name the tier that brought it in.
function noiseBy(
  entries: ContextEntry[],
  expect: string[],
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const entry of entries) {
    const path = place(entry);
    if (path !== null && expect.includes(path)) {
      continue;
    }
    const label =
      entry.origin === "search"
        ? "search"
        : entry.why
            .replace(/^indirect /, "")
            .split(" ")
            .slice(0, 2)
            .join(" ");
    counts[label] = (counts[label] ?? 0) + 1;
  }
  return counts;
}

async function scoreTool(
  pool: pg.Pool,
  project: string,
  q: Query,
): Promise<ToolScored | null> {
  const tool = KIND_TOOL[q.kind];
  if (tool === undefined || q.symbol === undefined) {
    return null;
  }
  let found: (string | null)[];
  if (tool === "impact") {
    const answer = await impactAnalysis(pool, [project], q.symbol, null, 3);
    found = [
      ...answer.resolved.map((node) => node.file_path),
      ...answer.impact.files,
    ];
  } else {
    const answer = await findSymbol(pool, [project], tool, q.symbol, null, 1);
    found = [
      ...answer.resolved.map((node) => node.file_path),
      ...answer.results.map((hit) => hit.file_path),
    ];
  }
  const files = [
    ...new Set(found.filter((path): path is string => path !== null)),
  ];
  const first = files.findIndex((path) => q.expect.includes(path));
  return {
    id: q.id,
    tool: tool === "impact" ? "impact_analysis" : tool,
    recall_at_5: recall(q.expect, files.slice(0, 5)),
    recall: recall(q.expect, files),
    mrr: first === -1 ? 0 : 1 / (first + 1),
    missed: q.expect.filter((path) => !files.includes(path)),
  };
}

function summarizeTools(rows: ToolScored[]): Record<string, object> {
  const tools = [...new Set(rows.map((row) => row.tool))].sort();
  return Object.fromEntries(
    tools.map((tool) => {
      const mine = rows.filter((row) => row.tool === tool);
      return [
        tool,
        {
          queries: mine.length,
          recall_at_5: round(mean(mine.map((row) => row.recall_at_5))),
          recall: round(mean(mine.map((row) => row.recall))),
          mrr: round(mean(mine.map((row) => row.mrr))),
        },
      ];
    }),
  );
}

function summarize(rows: Scored[]): Summary {
  const quality = Object.fromEntries(
    QUALITY.map((metric) => [
      metric,
      round(mean(rows.map((row) => row[metric]))),
    ]),
  ) as Record<Quality, number>;
  return {
    queries: rows.length,
    ...quality,
    tokens_mean: Math.round(mean(rows.map((row) => row.tokens))),
    search_ms_p50: round(
      percentile(
        rows.map((row) => row.search_ms),
        50,
      ),
    ),
    search_ms_p95: round(
      percentile(
        rows.map((row) => row.search_ms),
        95,
      ),
    ),
    context_ms_p50: round(
      percentile(
        rows.map((row) => row.context_ms),
        50,
      ),
    ),
    context_ms_p95: round(
      percentile(
        rows.map((row) => row.context_ms),
        95,
      ),
    ),
  };
}

function regressions(
  current: Summary,
  baseline: Summary,
  tolerance: number,
): string[] {
  return QUALITY.filter(
    (metric) => current[metric] < baseline[metric] - tolerance,
  ).map(
    (metric) =>
      `${metric}: ${current[metric]} < baseline ${baseline[metric]} - ${tolerance}`,
  );
}

async function main(): Promise<number> {
  const url = process.env.EVAL_DATABASE_URL;
  if (url === undefined) {
    console.error(
      "EVAL_DATABASE_URL is not set; start the eval stack with `make eval-up`",
    );
    return 2;
  }
  const opts = options(process.argv.slice(2));
  const suite = parse(
    readFileSync(resolve(EVAL_DIR, "queries.yaml"), "utf8"),
  ) as { project: string; queries: Query[] };
  const pool = new pg.Pool({ connectionString: url });

  const rows: Scored[] = [];
  const toolRows: ToolScored[] = [];
  let semantic = false;
  try {
    for (const q of suite.queries) {
      const result = await score(pool, suite.project, q, opts);
      semantic ||= result.semantic;
      rows.push(result.scored);
      const scored = await scoreTool(pool, suite.project, q);
      if (scored !== null) {
        toolRows.push(scored);
      }
    }
  } finally {
    await pool.end();
  }

  const mode = `${semantic ? "hybrid" : "lexical"}${opts.rerank ? "" : "-norerank"}`;
  const overall = summarize(rows);
  const kinds = [...new Set(rows.map((row) => row.kind))].sort();
  const byKind = Object.fromEntries(
    kinds.map((kind) => [
      kind,
      summarize(rows.filter((row) => row.kind === kind)),
    ]),
  );

  const byTool = summarizeTools(toolRows);
  const noise: Record<string, number> = {};
  for (const row of rows) {
    for (const [label, count] of Object.entries(row.noise)) {
      noise[label] = (noise[label] ?? 0) + count;
    }
  }

  const latest = resolve(EVAL_DIR, "results", `${mode}.json`);
  mkdirSync(dirname(latest), { recursive: true });
  writeFileSync(
    latest,
    JSON.stringify(
      {
        mode,
        overall,
        by_kind: byKind,
        by_tool: byTool,
        noise,
        queries: rows,
        tool_queries: toolRows,
      },
      null,
      2,
    ) + "\n",
  );

  console.log(`mode ${mode}, ${rows.length} queries`);
  console.table({ overall, ...byKind });
  for (const row of rows.filter((r) => r.missed.length > 0)) {
    console.log(`  ${row.id}: packet missed ${row.missed.join(", ")}`);
  }
  console.table(byTool);
  console.log("entries outside the expected files, by what brought them in");
  console.table(noise);
  for (const row of toolRows.filter((r) => r.missed.length > 0)) {
    console.log(`  ${row.id}: ${row.tool} missed ${row.missed.join(", ")}`);
  }

  const baselinePath = resolve(EVAL_DIR, `baseline.${mode}.json`);
  if (opts.updateBaseline) {
    writeFileSync(baselinePath, JSON.stringify(overall, null, 2) + "\n");
    console.log(`baseline written: ${baselinePath}`);
    return 0;
  }
  let baseline: Summary;
  try {
    baseline = JSON.parse(readFileSync(baselinePath, "utf8")) as Summary;
  } catch {
    console.log(
      `no baseline for ${mode}; run with --update-baseline to record one`,
    );
    return 0;
  }
  const failed = regressions(overall, baseline, opts.tolerance);
  for (const line of failed) {
    console.error(`REGRESSION ${line}`);
  }
  return failed.length === 0 ? 0 : 1;
}

main().then(
  (code) => process.exit(code),
  (error: unknown) => {
    console.error(error);
    process.exit(2);
  },
);
