import type pg from "pg";
import { isTestPath, lineFromId } from "./context.js";
import { knowledgeFor } from "./knowledge.js";
import type { Knowledge } from "./knowledge.js";
import { ancestorIds, linksInto } from "./links.js";
import type { LinkedNode } from "./links.js";
import { grepTrees } from "./worker.js";
import type { GrepMatch } from "./worker.js";

export const DEFAULT_SYMBOL_HOPS = 1;
export const MAX_SYMBOL_HOPS = 3;
export const DEFAULT_IMPACT_DEPTH = 3;
const TEST_HOPS = 2;
const MAX_RESOLVED = 10;
const EDGES_PER_STEP = 50;
const MAX_GRAPH_ROWS = 200;
const MAX_TEXT_LINES = 400;
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
  evidence: "graph" | "text" | "link";
  confidence: string | null;
  hop: number;
  via?: string | null;
  link?: {
    from: string;
    to: string;
    kind: string | null;
    name: string | null;
    origin: string;
  };
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
  /** Memories, plans and suggestions about what the change reaches. */
  knowledge: Knowledge[];
  notes: string[];
}

export interface TextPattern {
  pg: string;
  js: RegExp;
  /** The same pattern for ripgrep, which reads the mounted trees. */
  rg: string;
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
  const bounded = body.replace(/\\[mM]/g, "\\b");
  return {
    pg: `(?n)${body}`,
    js: new RegExp(bounded),
    rg: bounded,
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

export function bucketImpact(
  hits: SymbolHit[],
  crossProject: SymbolHit[] = [],
): ImpactBuckets {
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
      cross_project: crossProject.length,
      files: files.length,
    },
    files,
    direct: cap(direct),
    indirect: cap(indirect),
    tests: cap(tests),
    public_api: cap(publicApi),
    configuration: cap(configuration),
    cross_project: cap(crossProject),
  };
}

/** What other projects take from these nodes, their files or a directory above. */
export function crossProjectHits(rows: LinkedNode[]): SymbolHit[] {
  return rows.map((row) => ({
    project: row.source_project,
    id: row.source_id,
    name: row.node_name ?? row.source_id,
    type: row.node_type ?? "directory",
    file_path: row.file_path,
    line: lineFromId(row.source_id),
    relation: row.relation_type,
    evidence: "link",
    confidence: null,
    hop: 1,
    via: row.target_id,
    link: {
      from: row.source_project,
      to: row.target_project,
      kind: row.kind,
      name: row.name,
      origin: row.origin,
    },
  }));
}

export function linkTargets(
  nodes: { project: string; id: string; file_path?: string | null }[],
): { project: string; id: string }[] {
  const seen = new Set<string>();
  const targets: { project: string; id: string }[] = [];
  for (const node of nodes) {
    const file = node.file_path ?? null;
    const ids = [node.id, ...(file === null ? [] : [file])];
    for (const id of [...ids, ...ancestorIds(file ?? node.id)]) {
      const key = `${node.project}\0${id}`;
      if (!seen.has(key)) {
        seen.add(key);
        targets.push({ project: node.project, id });
      }
    }
  }
  return targets;
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
         FROM nodes AS n
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
         FROM nodes AS n
         LEFT JOIN LATERAL (
           SELECT p.name
             FROM edges AS e
             JOIN nodes AS p
               ON p.project = e.project AND p.id = e.source_id
            WHERE e.project = n.project AND e.target_id = n.id
              AND e.relation_type IN ('contains', 'method')
              AND p.type NOT IN ('file', 'directory')
            ORDER BY (p.name = $3::text) DESC, p.id
            LIMIT 1
         ) AS o ON TRUE
        WHERE n.project = ANY ($1::text[])
          AND n.type NOT IN ('file', 'directory')
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
             FROM edges AS e
            WHERE e.project = w.project
              AND (($3::text = 'incoming' AND e.target_id = w.node_id)
                OR ($3::text = 'outgoing' AND e.source_id = w.node_id))
              AND ($4::text[] IS NULL OR e.relation_type = ANY ($4::text[]))
              AND e.relation_type <> ALL ($5::text[])
            ORDER BY e.relation_type, e.source_id, e.target_id
            LIMIT $6
         ) AS step ON TRUE
        WHERE w.hop < $7 AND NOT step.id = ANY (w.path)
     )
     SELECT DISTINCT ON (w.project, w.node_id)
            w.project, w.node_id AS id, w.hop, w.via, w.relation,
            w.confidence, n.name, n.type, n.file_path
       FROM walk AS w
       JOIN nodes AS n ON n.project = w.project AND n.id = w.node_id
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
       JOIN edges AS e
         ON e.project = s.project AND e.source_id = s.id
        AND e.relation_type = ANY ($3::text[])
       JOIN nodes AS n
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

const TEXT_UNAVAILABLE =
  "No text evidence: the worker API that reads the mounted trees did not " +
  "answer, so the results are graph edges only";

/**
 * Lines of the mounted trees matching a pattern, folded into one hit per
 * file node. The trees are read where they are mounted; nothing here keeps
 * their text.
 */
async function textHits(
  pool: pg.Pool,
  projects: string[],
  pattern: TextPattern,
  relation: string,
  skip: Set<string>,
  notes: string[],
): Promise<SymbolHit[]> {
  let matches: GrepMatch[];
  try {
    matches = (
      await grepTrees({
        projects,
        pattern: pattern.rg,
        regex: true,
        loose: false,
        path: "",
        limit: MAX_TEXT_LINES,
      })
    ).matches;
  } catch {
    if (!notes.includes(TEXT_UNAVAILABLE)) {
      notes.push(TEXT_UNAVAILABLE);
    }
    return [];
  }
  if (matches.length === 0) {
    return [];
  }
  const res = await pool.query<{
    project: string;
    id: string;
    name: string;
    type: string;
    file_path: string;
  }>(
    `SELECT DISTINCT ON (n.project, n.file_path)
            n.project, n.id, n.name, n.type, n.file_path
       FROM nodes AS n
       JOIN unnest($1::text[], $2::text[]) AS w (project, path)
         ON n.project = w.project AND n.file_path = w.path
      WHERE n.type = 'file'
      ORDER BY n.project, n.file_path, n.id`,
    [matches.map((one) => one.project), matches.map((one) => one.path)],
  );
  const files = new Map(
    res.rows.map((row) => [`${row.project}\u0000${row.file_path}`, row]),
  );
  const byFile = new Map<string, SymbolHit>();
  for (const match of matches) {
    const key = `${match.project}\u0000${match.path}`;
    const node = files.get(key);
    if (
      node === undefined ||
      skip.has(lineKey(match.project, match.path, match.line))
    ) {
      continue;
    }
    const hit: SymbolHit = byFile.get(key) ?? {
      project: node.project,
      id: node.id,
      name: node.name,
      type: node.type,
      file_path: node.file_path,
      line: null,
      lines: [],
      relation,
      evidence: "text" as const,
      confidence: "NAME_MATCH",
      hop: 1,
    };
    if (!hit.lines?.includes(match.line)) {
      hit.lines?.push(match.line);
    }
    byFile.set(key, hit);
  }
  return [...byFile.values()].map((hit) => {
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

const CALLS_NOTE =
  "Calls across files are graph edges for Python, TypeScript and " +
  "JavaScript; in other languages a caller in another file is found by " +
  "text evidence (NAME_MATCH) alone.";

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
    pattern === null
      ? []
      : await textHits(pool, projects, pattern, relation, skip, notes);
  let results = mergeHits(graph, text);
  if (tool === "find_tests") {
    results = results.filter((hit) => isTestPath(hit.file_path));
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

  const skip = definitionLines(nodes);
  let text: SymbolHit[] = [];
  if (ref.path === null) {
    text = await textHits(
      pool,
      projects,
      wordPattern([ref.name]),
      "mentions",
      skip,
      notes,
    );
  } else {
    const own = new Set(nodes.map((node) => node.file_path));
    const importers = await textHits(
      pool,
      projects,
      importPattern(ref.name),
      "imports",
      skip,
      notes,
    );
    const names = [...new Set(defined.map((node) => bareName(node.name)))]
      .filter((name) => name.length > 2)
      .slice(0, NAMES_PER_FILE);
    const users =
      names.length === 0
        ? []
        : await textHits(
            pool,
            projects,
            wordPattern(names),
            "mentions",
            skip,
            notes,
          );
    text = [...importers, ...users].filter((hit) => !own.has(hit.file_path));
  }
  notes.push(CALLS_NOTE);
  const hits = mergeHits(graph, text).filter(
    (hit) =>
      !seeds.some((seed) => seed.project === hit.project && seed.id === hit.id),
  );
  // Text matches are leads, not dependents, so only graph hits reach out.
  const reached = [...seeds, ...hits.filter((hit) => hit.evidence === "graph")];
  const crossProject = crossProjectHits(
    await linksInto(pool, linkTargets(reached)),
  );
  const knowledge = await knowledgeFor(
    pool,
    reached.map((one) => ({ project: one.project, node_id: one.id })),
  );
  return {
    symbol,
    resolved: nodes,
    impact: bucketImpact(hits, crossProject),
    knowledge,
    notes,
  };
}
