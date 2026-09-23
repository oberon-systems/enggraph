import type pg from "pg";
import { isTestPath, lineFromId } from "./context.js";

export const DEFAULT_SYMBOL_HOPS = 1;
export const MAX_SYMBOL_HOPS = 3;
export const DEFAULT_IMPACT_DEPTH = 3;
const TEST_HOPS = 2;
const MAX_RESOLVED = 10;
const EDGES_PER_STEP = 50;
const MAX_GRAPH_ROWS = 200;
const MAX_TEXT_CHUNKS = 400;
const LINES_PER_FILE = 10;
const BUCKET_CAP = 25;
const NAMES_PER_FILE = 10;

const CALLS = ["calls"];
const INHERITS = ["inherits", "extends", "implements"];
const MEMBERSHIP = ["contains", "method"];
const CONFIG_RELATIONS = new Set([
  "uses_file",
  "uses_template",
  "reads_vars",
  "uses_role",
  "mounts",
  "builds",
  "notifies",
]);

const PATH_LIKE =
  /\/|\.(ts|tsx|js|jsx|mjs|cjs|py|go|rs|rb|java|kt|cs|php|c|cc|cpp|h|hpp|scala|ya?ml|json|toml|tf|hcl|sh|md)$/i;
const PUBLIC_API_PATH =
  /(^|\/)(routes?|controllers?|handlers?|api|server|endpoints?)([./_-]|$)/i;
const CONFIG_PATH =
  /(^|\/)(dockerfile[^/]*|docker-compose[^/]*|config[^/]*|settings[^/]*|[^/]*\.(ya?ml|json|toml|ini|cfg|conf|env|tf|hcl|j2))$/i;
const CALLABLE_TYPES = new Set(["function", "method"]);

export type SymbolTool =
  | "find_callers"
  | "find_callees"
  | "find_references"
  | "find_implementations"
  | "find_tests";

export interface SymbolRef {
  owner: string | null;
  name: string;
  path: string | null;
}

export interface ResolvedNode {
  project: string;
  id: string;
  name: string;
  type: string;
  file_path: string | null;
  line: number | null;
  owner: string | null;
  summary: string | null;
}

export interface SymbolHit {
  project: string;
  id: string;
  name: string;
  type: string;
  file_path: string | null;
  line: number | null;
  lines?: number[];
  relation: string;
  evidence: "graph" | "text";
  confidence: string | null;
  hop: number;
  via?: string | null;
}

export interface SymbolAnswer {
  symbol: string;
  resolved: ResolvedNode[];
  results: SymbolHit[];
  notes: string[];
}

export interface ImpactBuckets {
  counts: Record<
    | "direct"
    | "indirect"
    | "tests"
    | "public_api"
    | "configuration"
    | "cross_project"
    | "files",
    number
  >;
  files: string[];
  direct: SymbolHit[];
  indirect: SymbolHit[];
  tests: SymbolHit[];
  public_api: SymbolHit[];
  configuration: SymbolHit[];
  cross_project: SymbolHit[];
}

export interface ImpactAnswer {
  symbol: string;
  resolved: ResolvedNode[];
  impact: ImpactBuckets;
  notes: string[];
}

export interface TextPattern {
  pg: string;
  js: RegExp;
}

interface WalkOptions {
  include: string[] | null;
  exclude: string[];
  direction: "incoming" | "outgoing";
  hops: number;
}

/** Strip the decoration the upstream extractor puts on a label. */
export function bareName(label: string): string {
  return label.replace(/^\.+/, "").replace(/\(\)$/, "");
}

export function parseSymbol(text: string): SymbolRef {
  const trimmed = text.trim();
  if (PATH_LIKE.test(trimmed)) {
    const base = trimmed.slice(trimmed.lastIndexOf("/") + 1);
    const dot = base.indexOf(".");
    return {
      owner: null,
      name: dot > 0 ? base.slice(0, dot) : base,
      path: trimmed.replace(/^\.\//, ""),
    };
  }
  const plain = bareName(trimmed);
  const parts = plain.split(/::|#|\./).filter((part) => part !== "");
  const name = parts.pop() ?? plain;
  return { owner: parts.pop() ?? null, name, path: null };
}

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Written once in PostgreSQL's ARE dialect; `(?n)` keeps `.` on one line, as
// JavaScript does, since the JS copy is run line by line.
function textPattern(body: string): TextPattern {
  return {
    pg: `(?n)${body}`,
    js: new RegExp(body.replace(/\\[mM]/g, "\\b")),
  };
}

function alternation(names: string[]): string {
  const unique = [...new Set(names.filter((name) => /\w/.test(name)))];
  if (unique.length === 0) {
    throw new Error(`No identifier to match in "${names.join(", ")}"`);
  }
  return unique.length === 1
    ? escapeRegex(unique[0])
    : `(${unique.map(escapeRegex).join("|")})`;
}

export function wordPattern(names: string[]): TextPattern {
  return textPattern(`\\m${alternation(names)}\\M`);
}

export function callPattern(names: string[]): TextPattern {
  return textPattern(`\\m${alternation(names)}\\M\\s*\\(`);
}

export function implementationPattern(name: string): TextPattern {
  const target = escapeRegex(name);
  return textPattern(
    `\\m(extends|implements)\\s+([\\w.]+\\s*,\\s*)*${target}\\M` +
      `|\\mclass\\s+\\w+\\s*\\([^)]*\\m${target}\\M`,
  );
}

export function importPattern(stem: string): TextPattern {
  return textPattern(`\\m(import|from|require)\\M.*\\m${escapeRegex(stem)}\\M`);
}

function lineKey(project: string, file: string | null, line: number): string {
  return `${project}\u0000${file ?? ""}\u0000${line}`;
}

/**
 * Graph hits first, one per node; a text hit keeps only the lines no graph
 * hit already stands on, and is dropped when none are left.
 */
export function mergeHits(graph: SymbolHit[], text: SymbolHit[]): SymbolHit[] {
  const seen = new Set<string>();
  const taken = new Set<string>();
  const merged: SymbolHit[] = [];
  for (const hit of graph) {
    const key = `${hit.project}\u0000${hit.id}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(hit);
    if (hit.line !== null) {
      taken.add(lineKey(hit.project, hit.file_path, hit.line));
    }
  }
  const files = new Map<string, SymbolHit>();
  for (const hit of text) {
    const key = `${hit.project}\u0000${hit.id}`;
    const into: SymbolHit = files.get(key) ?? { ...hit, lines: [] };
    for (const line of hit.lines ?? []) {
      if (
        !taken.has(lineKey(hit.project, hit.file_path, line)) &&
        !into.lines?.includes(line)
      ) {
        into.lines?.push(line);
      }
    }
    files.set(key, into);
  }
  for (const hit of files.values()) {
    const lines = [...(hit.lines ?? [])].sort((a, b) => a - b);
    if (lines.length > 0) {
      merged.push({ ...hit, line: lines[0], lines });
    }
  }
  return merged;
}

export function bucketImpact(hits: SymbolHit[]): ImpactBuckets {
  const direct = hits.filter((hit) => hit.hop <= 1);
  const indirect = hits.filter((hit) => hit.hop > 1);
  const tests = hits.filter((hit) => isTestPath(hit.file_path));
  const rest = hits.filter((hit) => !isTestPath(hit.file_path));
  const publicApi = rest.filter(
    (hit) => hit.file_path !== null && PUBLIC_API_PATH.test(hit.file_path),
  );
  const configuration = rest.filter(
    (hit) =>
      CONFIG_RELATIONS.has(hit.relation) ||
      (hit.file_path !== null && CONFIG_PATH.test(hit.file_path)),
  );
  const files = [
    ...new Set(
      [...hits]
        .sort((a, b) => a.hop - b.hop)
        .map((hit) => hit.file_path)
        .filter((path): path is string => path !== null),
    ),
  ];
  const cap = (list: SymbolHit[]) => list.slice(0, BUCKET_CAP);
  return {
    counts: {
      direct: direct.length,
      indirect: indirect.length,
      tests: tests.length,
      public_api: publicApi.length,
      configuration: configuration.length,
      cross_project: 0,
      files: files.length,
    },
    files,
    direct: cap(direct),
    indirect: cap(indirect),
    tests: cap(tests),
    public_api: cap(publicApi),
    configuration: cap(configuration),
    cross_project: [],
  };
}

async function resolveSymbol(
  pool: pg.Pool,
  members: string[],
  ref: SymbolRef,
  filePath: string | null,
): Promise<{ nodes: ResolvedNode[]; notes: string[] }> {
  const notes: string[] = [];
  let rows: Omit<ResolvedNode, "line">[];
  if (ref.path !== null) {
    const res = await pool.query<Omit<ResolvedNode, "line">>(
      `SELECT n.project, n.id, n.name, n.type, n.file_path, n.summary,
              NULL::text AS owner
         FROM graph_nodes AS n
        WHERE n.project = ANY ($1::text[])
          AND n.type = 'file'
          AND (n.id = $2 OR right(n.id, length($2) + 1) = '/' || $2)
        ORDER BY n.project, length(n.id), n.id
        LIMIT $3`,
      [members, ref.path, MAX_RESOLVED],
    );
    rows = res.rows;
  } else {
    const res = await pool.query<Omit<ResolvedNode, "line">>(
      `SELECT n.project, n.id, n.name, n.type, n.file_path, n.summary,
              o.name AS owner
         FROM graph_nodes AS n
         LEFT JOIN LATERAL (
           SELECT p.name
             FROM graph_edges AS e
             JOIN graph_nodes AS p
               ON p.project = e.project AND p.id = e.source_id
            WHERE e.project = n.project AND e.target_id = n.id
              AND e.relation_type IN ('contains', 'method')
              AND p.type <> 'file'
            ORDER BY (p.name = $3::text) DESC, p.id
            LIMIT 1
         ) AS o ON TRUE
        WHERE n.project = ANY ($1::text[])
          AND n.type <> 'file'
          AND NOT starts_with(n.type, 'external_')
          AND regexp_replace(ltrim(n.name, '.'), '[(][)]$', '') = $2
          AND ($4::text IS NULL OR n.file_path = $4
               OR right(n.file_path, length($4) + 1) = '/' || $4)
        ORDER BY (o.name IS NOT DISTINCT FROM $3::text) DESC,
                 n.project, n.file_path, n.id
        LIMIT $5`,
      [members, ref.name, ref.owner, filePath, MAX_RESOLVED],
    );
    rows = res.rows;
    if (ref.owner !== null && rows.length > 0) {
      const owned = rows.filter((row) => row.owner === ref.owner);
      if (owned.length > 0) {
        rows = owned;
      } else {
        notes.push(
          `No "${ref.name}" is linked to "${ref.owner}" in the graph; ` +
            `answering for every node named "${ref.name}".`,
        );
      }
    }
  }
  if (rows.length === 0) {
    notes.push(
      `No node resolves "${ref.path ?? ref.name}"; the extractor may not ` +
        "declare it (an interface or a type often is not). Text evidence " +
        "is still searched by name. search_code_nodes matches by substring.",
    );
  } else if (rows.length > 1) {
    notes.push(
      `${rows.length} nodes match; pass file_path to answer for one of them.`,
    );
  }
  return {
    nodes: rows.map((row) => ({ ...row, line: lineFromId(row.id) })),
    notes,
  };
}

async function walkGraph(
  pool: pg.Pool,
  seeds: { project: string; id: string }[],
  options: WalkOptions,
): Promise<SymbolHit[]> {
  if (seeds.length === 0 || options.hops < 1) {
    return [];
  }
  const res = await pool.query<{
    project: string;
    id: string;
    hop: number;
    via: string | null;
    relation: string;
    confidence: string | null;
    name: string;
    type: string;
    file_path: string | null;
  }>(
    `WITH RECURSIVE walk(project, node_id, hop, via, relation, confidence,
                        path) AS (
       SELECT s.project, s.id, 0, NULL::text, NULL::text, NULL::text,
              ARRAY[s.id]
         FROM UNNEST($1::text[], $2::text[]) AS s(project, id)
       UNION ALL
       SELECT w.project, step.id, w.hop + 1, w.node_id, step.relation,
              step.confidence, w.path || step.id
         FROM walk AS w
         JOIN LATERAL (
           SELECT (CASE WHEN $3::text = 'incoming' THEN e.source_id
                        ELSE e.target_id END)::text AS id,
                  e.relation_type::text AS relation,
                  e.metadata ->> 'confidence' AS confidence
             FROM graph_edges AS e
            WHERE e.project = w.project
              AND (($3::text = 'incoming' AND e.target_id = w.node_id)
                OR ($3::text = 'outgoing' AND e.source_id = w.node_id))
              AND ($4::text[] IS NULL OR e.relation_type = ANY ($4::text[]))
              AND e.relation_type <> ALL ($5::text[])
            ORDER BY e.relation_type, e.id
            LIMIT $6
         ) AS step ON TRUE
        WHERE w.hop < $7 AND NOT step.id = ANY (w.path)
     )
     SELECT DISTINCT ON (w.project, w.node_id)
            w.project, w.node_id AS id, w.hop, w.via, w.relation,
            w.confidence, n.name, n.type, n.file_path
       FROM walk AS w
       JOIN graph_nodes AS n ON n.project = w.project AND n.id = w.node_id
      WHERE w.hop > 0 AND NOT starts_with(n.type, 'external_')
      ORDER BY w.project, w.node_id, w.hop
      LIMIT $8`,
    [
      seeds.map((seed) => seed.project),
      seeds.map((seed) => seed.id),
      options.direction,
      options.include,
      options.exclude,
      EDGES_PER_STEP,
      options.hops,
      MAX_GRAPH_ROWS,
    ],
  );
  return res.rows
    .map((row) => ({
      project: row.project,
      id: row.id,
      name: row.name,
      type: row.type,
      file_path: row.file_path,
      line: lineFromId(row.id),
      relation: row.relation,
      evidence: "graph" as const,
      confidence: row.confidence,
      hop: row.hop,
      via: row.via,
    }))
    .sort((a, b) => a.hop - b.hop || a.id.localeCompare(b.id));
}

async function members(
  pool: pg.Pool,
  seeds: ResolvedNode[],
): Promise<{ project: string; id: string; name: string }[]> {
  if (seeds.length === 0) {
    return [];
  }
  const res = await pool.query<{ project: string; id: string; name: string }>(
    `SELECT e.project, e.target_id AS id, n.name
       FROM UNNEST($1::text[], $2::text[]) AS s(project, id)
       JOIN graph_edges AS e
         ON e.project = s.project AND e.source_id = s.id
        AND e.relation_type = ANY ($3::text[])
       JOIN graph_nodes AS n
         ON n.project = e.project AND n.id = e.target_id
      WHERE NOT starts_with(n.type, 'external_')
      ORDER BY e.project, e.target_id`,
    [
      seeds.map((seed) => seed.project),
      seeds.map((seed) => seed.id),
      MEMBERSHIP,
    ],
  );
  return res.rows;
}

async function chunkless(pool: pg.Pool, projects: string[]): Promise<string[]> {
  const res = await pool.query<{ project: string }>(
    `SELECT m.project
       FROM UNNEST($1::text[]) AS m(project)
      WHERE NOT EXISTS (
              SELECT 1 FROM code_embeddings AS e WHERE e.project = m.project
            )
      ORDER BY m.project`,
    [projects],
  );
  return res.rows.map((row) => row.project);
}

async function textHits(
  pool: pg.Pool,
  projects: string[],
  pattern: TextPattern,
  relation: string,
  skip: Set<string>,
): Promise<SymbolHit[]> {
  const res = await pool.query<{
    project: string;
    id: string;
    name: string;
    type: string;
    file_path: string | null;
    start_line: number;
    content_chunk: string;
  }>(
    `SELECT e.project, e.node_id AS id, n.name, n.type, n.file_path,
            e.start_line, e.content_chunk
       FROM code_embeddings AS e
       JOIN graph_nodes AS n ON n.project = e.project AND n.id = e.node_id
      WHERE e.project = ANY ($1::text[]) AND e.content_chunk ~ $2
      ORDER BY e.project, n.file_path, e.start_line
      LIMIT $3`,
    [projects, pattern.pg, MAX_TEXT_CHUNKS],
  );
  const byFile = new Map<string, SymbolHit>();
  for (const row of res.rows) {
    const key = `${row.project}\u0000${row.id}`;
    const hit: SymbolHit = byFile.get(key) ?? {
      project: row.project,
      id: row.id,
      name: row.name,
      type: row.type,
      file_path: row.file_path,
      line: null,
      lines: [],
      relation,
      evidence: "text" as const,
      confidence: "NAME_MATCH",
      hop: 1,
    };
    row.content_chunk.split("\n").forEach((text, offset) => {
      const line = row.start_line + offset;
      if (
        pattern.js.test(text) &&
        !skip.has(lineKey(row.project, row.file_path, line)) &&
        !hit.lines?.includes(line)
      ) {
        hit.lines?.push(line);
      }
    });
    byFile.set(key, hit);
  }
  return [...byFile.values()]
    .filter((hit) => (hit.lines?.length ?? 0) > 0)
    .map((hit) => {
      const lines = [...(hit.lines ?? [])]
        .sort((a, b) => a - b)
        .slice(0, LINES_PER_FILE);
      return { ...hit, line: lines[0], lines };
    });
}

function definitionLines(nodes: ResolvedNode[]): Set<string> {
  return new Set(
    nodes
      .filter((node) => node.line !== null)
      .map((node) => lineKey(node.project, node.file_path, node.line ?? 0)),
  );
}

function textNote(bare: string[]): string[] {
  return bare.length === 0
    ? []
    : [
        `${bare.join(", ")} carries no chunks, so the answer there is graph ` +
          "edges only: text evidence needs embedding switched on for the " +
          "project in the dashboard settings.",
      ];
}

const CALLS_NOTE =
  "The extractor resolves calls within one file only; a caller in another " +
  "file is found by text evidence (NAME_MATCH), never by a graph edge.";

export async function findDefinition(
  pool: pg.Pool,
  projects: string[],
  symbol: string,
  filePath: string | null,
): Promise<SymbolAnswer> {
  const found = await resolveSymbol(
    pool,
    projects,
    parseSymbol(symbol),
    filePath,
  );
  return { symbol, resolved: found.nodes, results: [], notes: found.notes };
}

export async function findSymbol(
  pool: pg.Pool,
  projects: string[],
  tool: SymbolTool,
  symbol: string,
  filePath: string | null,
  hops: number,
): Promise<SymbolAnswer> {
  const ref = parseSymbol(symbol);
  const found = await resolveSymbol(pool, projects, ref, filePath);
  const nodes = found.nodes;
  const names = [ref.name];
  const skip = definitionLines(nodes);
  const notes = [...found.notes];
  const bare = await chunkless(pool, projects);
  const withText = projects.filter((project) => !bare.includes(project));
  const callable =
    nodes.length === 0
      ? ref.owner !== null || /^[a-z_]/.test(ref.name)
      : nodes.some((node) => CALLABLE_TYPES.has(node.type));

  let graph: SymbolHit[] = [];
  let pattern: TextPattern | null = null;
  let relation = "mentions";

  if (tool === "find_callers") {
    graph = await walkGraph(pool, nodes, {
      include: CALLS,
      exclude: [],
      direction: "incoming",
      hops,
    });
    pattern = callable ? callPattern(names) : wordPattern(names);
    relation = callable ? "calls" : "mentions";
    notes.push(CALLS_NOTE);
  } else if (tool === "find_callees") {
    const seeds = nodes.some((node) => !CALLABLE_TYPES.has(node.type))
      ? [...nodes, ...(await members(pool, nodes))]
      : nodes;
    graph = await walkGraph(pool, seeds, {
      include: CALLS,
      exclude: [],
      direction: "outgoing",
      hops,
    });
    notes.push(
      "Callees come from graph edges alone, so only calls into the same " +
        "file are listed.",
    );
  } else if (tool === "find_references") {
    graph = await walkGraph(pool, nodes, {
      include: null,
      exclude: MEMBERSHIP,
      direction: "incoming",
      hops,
    });
    pattern = wordPattern(names);
  } else if (tool === "find_implementations") {
    graph = await walkGraph(pool, nodes, {
      include: INHERITS,
      exclude: [],
      direction: "incoming",
      hops,
    });
    pattern = implementationPattern(ref.name);
    relation = "implements";
  } else {
    graph = await walkGraph(pool, nodes, {
      include: null,
      exclude: MEMBERSHIP,
      direction: "incoming",
      hops: Math.max(hops, TEST_HOPS),
    });
    pattern = wordPattern(ref.owner === null ? names : [...names, ref.owner]);
  }

  const text =
    pattern === null || withText.length === 0
      ? []
      : await textHits(pool, withText, pattern, relation, skip);
  let results = mergeHits(graph, text);
  if (tool === "find_tests") {
    results = results.filter((hit) => isTestPath(hit.file_path));
  }
  if (pattern !== null) {
    notes.push(...textNote(bare));
  }
  return { symbol, resolved: nodes, results, notes };
}

export async function impactAnalysis(
  pool: pg.Pool,
  projects: string[],
  symbol: string,
  filePath: string | null,
  depth: number,
): Promise<ImpactAnswer> {
  const ref = parseSymbol(symbol);
  const found = await resolveSymbol(pool, projects, ref, filePath);
  const nodes = found.nodes;
  const notes = [...found.notes];
  const defined = ref.path === null ? [] : await members(pool, nodes);
  const seeds = [...nodes, ...defined];

  const graph = await walkGraph(pool, seeds, {
    include: null,
    exclude: MEMBERSHIP,
    direction: "incoming",
    hops: depth,
  });

  const bare = await chunkless(pool, projects);
  const withText = projects.filter((project) => !bare.includes(project));
  const skip = definitionLines(nodes);
  let text: SymbolHit[] = [];
  if (withText.length > 0) {
    if (ref.path === null) {
      text = await textHits(
        pool,
        withText,
        wordPattern([ref.name]),
        "mentions",
        skip,
      );
    } else {
      const own = new Set(nodes.map((node) => node.file_path));
      const importers = await textHits(
        pool,
        withText,
        importPattern(ref.name),
        "imports",
        skip,
      );
      const names = [...new Set(defined.map((node) => bareName(node.name)))]
        .filter((name) => name.length > 2)
        .slice(0, NAMES_PER_FILE);
      const users =
        names.length === 0
          ? []
          : await textHits(
              pool,
              withText,
              wordPattern(names),
              "mentions",
              skip,
            );
      text = [...importers, ...users].filter((hit) => !own.has(hit.file_path));
    }
  }
  notes.push(...textNote(bare));
  notes.push(
    "cross_project stays empty: no edge crosses a project yet.",
    CALLS_NOTE,
  );
  const hits = mergeHits(graph, text).filter(
    (hit) =>
      !seeds.some((seed) => seed.project === hit.project && seed.id === hit.id),
  );
  return { symbol, resolved: nodes, impact: bucketImpact(hits), notes };
}
