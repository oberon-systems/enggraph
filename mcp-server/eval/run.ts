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
import { rerank } from "../src/rerank.js";
import { hybridSearch } from "../src/search.js";

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
}

interface Scored extends Record<Quality, number> {
  id: string;
  kind: string;
  tokens: number;
  search_ms: number;
  context_ms: number;
  missed: string[];
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
  });
  const ranked = rerank(found.rows, q.query, SEARCH_LIMIT, opts.rerank);
  const searchMs = performance.now() - started;
  const files = [
    ...new Set(
      ranked
        .map(({ row }) => row.file_path)
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
  });
  const contextMs = performance.now() - started;
  const inPacket = packet.entries
    .map((entry) => entry.file_path)
    .filter((path): path is string => path !== null);
  const relevant = packet.entries.filter(
    (entry) => entry.file_path !== null && q.expect.includes(entry.file_path),
  );

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
    },
  };
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
  let semantic = false;
  try {
    for (const q of suite.queries) {
      const result = await score(pool, suite.project, q, opts);
      semantic ||= result.semantic;
      rows.push(result.scored);
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

  const latest = resolve(EVAL_DIR, "results", `${mode}.json`);
  mkdirSync(dirname(latest), { recursive: true });
  writeFileSync(
    latest,
    JSON.stringify({ mode, overall, by_kind: byKind, queries: rows }, null, 2) +
      "\n",
  );

  console.log(`mode ${mode}, ${rows.length} queries`);
  console.table({ overall, ...byKind });
  for (const row of rows.filter((r) => r.missed.length > 0)) {
    console.log(`  ${row.id}: packet missed ${row.missed.join(", ")}`);
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
