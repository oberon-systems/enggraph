import type pg from "pg";
import { identifiers, rerank } from "./rerank.js";
import { hybridSearch, semanticNote } from "./search.js";
import type { SearchRow } from "./search.js";

export const DEFAULT_TOKEN_BUDGET = 12000;
export const MAX_TOKEN_BUDGET = 60000;
export const MIN_TOKEN_BUDGET = 200;
export const DEFAULT_SEEDS = 8;
export const MAX_SEEDS = 25;
export const MAX_HOPS = 2;
// What one seed may pull in per round. A hub node has hundreds of edges and
// none of them are worth the whole budget.
const NEIGHBOURS_PER_SEED = 40;
// And what one seed may keep per tier. A 1500-line module defines fifty
// functions; the packet wants a map of it, not the whole index.
const PER_SEED_CAP: Record<Tier, number> = {
  caller: 8,
  test: 4,
  defines: 8,
  callee: 8,
  import: 3,
  container: 2,
  ancestor: 16,
};
// Importers share the caller tier but not its budget, and decay like imports:
// once imports resolve to real files, a hub's neighbours would fill the packet.
const IMPORTER_CAP = PER_SEED_CAP.import;
// How far up the directory ladder a summary packet climbs from a seed.
const MAX_ANCESTOR_DEPTH = 16;
// Below this budget source text cannot fit beside a map, so `auto` picks one.
const SUMMARY_BUDGET = 3000;
// A summary packet spends source text on this many of its best seeds only.
const SUMMARY_CHUNK_SEEDS = 2;
// The packet is text, and a token is roughly four characters of it. An
// estimate on purpose: the server holds no tokenizer, and the caller's model
// does not share one with every other caller's.
const CHARS_PER_TOKEN = 4;

export type Tier =
  | "caller"
  | "test"
  | "defines"
  | "callee"
  | "import"
  | "container"
  | "ancestor";

export type Detail = "auto" | "summary" | "source";

/** Hops to follow per tier; 0 switches a tier off. `container` is fixed. */
export type ExpandOptions = Record<
  "caller" | "callee" | "test" | "import" | "defines",
  number
>;

export const DEFAULT_EXPAND: ExpandOptions = {
  caller: 1,
  callee: 1,
  test: 1,
  import: 1,
  defines: 1,
};

// The order tiers are spent in: what depends on this explains it better than
// what it depends on, a test explains it better than an import, and what a
// matched file defines is the substance of the file itself.
const TIER_ORDER: Tier[] = [
  "caller",
  "test",
  "defines",
  "callee",
  "import",
  "container",
  "ancestor",
];

// A broad question is answered by the map first: where a hit sits, then what
// sits beside it, and only then what calls it.
const SUMMARY_TIER_ORDER: Tier[] = [
  "ancestor",
  "defines",
  "container",
  "caller",
  "test",
  "callee",
  "import",
];

// The vocabulary is open - the upstream extractor and the parsers each emit
// their own - so this set names the dependency edges that are known and
// everything else is placed by its direction alone.
const IMPORT_RELATIONS = new Set([
  "imports",
  "imports_from",
  "depends_on",
  "includes",
  "extends",
  "requires",
]);

const TEST_PATH =
  /(^|\/)(tests?|spec|specs)\/|(^|\/)test_[^/]*$|[._-](test|spec)\.[^/]+$|_test\.[^/]+$/i;

export interface ContextRequest {
  query: string;
  named: string | null;
  kind: string | null;
  seeds: number;
  tokenBudget: number;
  expand: ExpandOptions;
  includeChunks: boolean;
  rerankEnabled: boolean;
  detail?: Detail;
}

export interface ContextEntry {
  project?: string;
  project_type?: string;
  id: string;
  name: string;
  type: string;
  file_path: string | null;
  start_line: number | null;
  end_line: number | null;
  origin: "search" | "expansion";
  why: string;
  score?: number;
  summary: string | null;
  chunk?: string;
}

export interface ContextRelationship {
  project?: string;
  from: string;
  relation: string;
  to: string;
  direction: "incoming" | "outgoing";
}

export interface ContextPacket {
  query: string;
  detail: "summary" | "source";
  projects: string[];
  budget: { limit: number; used: number; truncated: boolean };
  entries: ContextEntry[];
  relationships: ContextRelationship[];
  notes: string[];
}

interface NeighbourRow {
  project: string;
  project_type: string;
  seed_id: string;
  node_id: string;
  relation_type: string;
  direction: "incoming" | "outgoing";
  name: string;
  type: string;
  file_path: string | null;
  summary: string | null;
  start_line: number | null;
  end_line: number | null;
  chunk: string | null;
}

/** One candidate for the packet, before the budget decides. */
export interface Candidate {
  key: string;
  entry: ContextEntry;
  tier: Tier | null;
  chunk: string | null;
  /** Rank of the seed this was reached from, best first. */
  seedOrder: number;
  /** Place within that seed's tier, so the tiers can be interleaved. */
  place: number;
}

export function estimateTokens(text: string): number {
  return Math.ceil(text.length / CHARS_PER_TOKEN);
}

export function isTestPath(path: string | null): boolean {
  return path !== null && TEST_PATH.test(path);
}

/**
 * Which tier an edge puts a neighbour in.
 *
 * A search hit is usually a file - the chunks are keyed to file nodes - so
 * what the file defines is the substance of the hit rather than noise, and
 * being imported by something is this graph's nearest thing to a caller.
 */
export function classify(
  relationType: string,
  direction: "incoming" | "outgoing",
  filePath: string | null,
): Tier | null {
  if (relationType === "contains") {
    return direction === "incoming" ? "container" : "defines";
  }
  if (isTestPath(filePath)) {
    return "test";
  }
  if (IMPORT_RELATIONS.has(relationType)) {
    return direction === "incoming" ? "caller" : "import";
  }
  return direction === "incoming" ? "caller" : "callee";
}

/** An edge that reached a caller only by importing the hit's file. */
export function isImporter(
  relationType: string,
  direction: "incoming" | "outgoing",
): boolean {
  return direction === "incoming" && IMPORT_RELATIONS.has(relationType);
}

// What a hit at rank `seedOrder` may keep in a tier. The bulk tiers decay
// with the rank: a map of the best hit's file is context, the same map of the
// eighth hit is a table of contents nobody asked for. What depends on a hit,
// and the tests near it, are scarce enough to stay flat.
export function capFor(
  tier: Tier,
  seedOrder: number,
  importer = false,
): number {
  if (importer) {
    return Math.max(IMPORTER_CAP - seedOrder, 0);
  }
  if (tier === "defines") {
    return Math.max(PER_SEED_CAP[tier] - seedOrder * 2, 0);
  }
  if (tier === "import") {
    return Math.max(PER_SEED_CAP[tier] - seedOrder, 0);
  }
  return PER_SEED_CAP[tier];
}

function hopsFor(tier: Tier, expand: ExpandOptions): number {
  return tier === "container" || tier === "ancestor" ? 1 : expand[tier];
}

/**
 * Settle `auto`: a question naming code gets source, a question in words gets
 * the summary ladder, and so does a budget too small for source beside a map.
 */
export function resolveDetail(
  detail: Detail,
  query: string,
  tokenBudget: number,
): "summary" | "source" {
  if (detail !== "auto") {
    return detail;
  }
  if (tokenBudget < SUMMARY_BUDGET) {
    return "summary";
  }
  const named =
    [...identifiers(query).values()].some((shaped) => shaped) ||
    /[\w-]+\/[\w.-]+|\w\(\)/.test(query);
  return named ? "source" : "summary";
}

// A symbol node carries its line in its id (`path::Name@L70`); nothing else
// records it, and a reference without a line is a file to search by hand.
export function lineFromId(id: string): number | null {
  const found = /@L(\d+)$/.exec(id);
  return found === null ? null : Number(found[1]);
}

// A seed is named by the tail of its id: the entry beside it already carries
// the whole path, and on a vendored tree that path is forty tokens of it.
export function shorten(id: string): string {
  const cut = id.indexOf("::");
  const path = cut === -1 ? id : id.slice(0, cut);
  const slash = path.lastIndexOf("/");
  return (slash === -1 ? path : path.slice(slash + 1)) + id.slice(path.length);
}

function why(tier: Tier, relationType: string, seedName: string): string {
  const what =
    tier === "ancestor"
      ? "holds"
      : tier === "container"
        ? "contained in"
        : tier === "defines"
          ? "defined in"
          : tier === "test"
            ? "test near"
            : tier === "caller"
              ? `${relationType} into`
              : `${relationType} from`;
  return `${what} ${seedName}`;
}

interface AncestorRow {
  project: string;
  project_type: string;
  seed_id: string;
  node_id: string;
  depth: number;
  name: string;
  type: string;
  file_path: string | null;
  summary: string | null;
}

// Up the `contains` edges from each seed: its file, then every directory up
// to the repository root, each carrying only its summary.
async function fetchAncestors(
  pool: pg.Pool,
  frontier: { project: string; id: string }[],
): Promise<AncestorRow[]> {
  if (frontier.length === 0) {
    return [];
  }
  const res = await pool.query<AncestorRow>(
    `WITH RECURSIVE up AS (
       SELECT s.project, s.id AS seed_id, s.id AS node_id, 0 AS depth
         FROM UNNEST($1::text[], $2::text[]) AS s(project, id)
       UNION
       SELECT u.project, u.seed_id, e.source_id, u.depth + 1
         FROM up AS u
         JOIN graph_edges AS e
           ON e.project = u.project AND e.target_id = u.node_id
          AND e.relation_type = 'contains'
        WHERE u.depth < $3
     )
     SELECT u.project, p.type AS project_type, u.seed_id, u.node_id,
            u.depth, n.name, n.type, n.file_path, n.summary
       FROM up AS u
       JOIN graph_nodes AS n ON n.project = u.project AND n.id = u.node_id
       JOIN projects AS p ON p.name = u.project
      WHERE u.depth > 0 AND NOT starts_with(n.type, 'external_')
      ORDER BY u.project, u.seed_id, u.depth, u.node_id`,
    [
      frontier.map((seed) => seed.project),
      frontier.map((seed) => seed.id),
      MAX_ANCESTOR_DEPTH,
    ],
  );
  return res.rows;
}

async function fetchNeighbours(
  pool: pg.Pool,
  frontier: { project: string; id: string }[],
): Promise<NeighbourRow[]> {
  if (frontier.length === 0) {
    return [];
  }
  const res = await pool.query<NeighbourRow>(
    // The project is carried through the CTE rather than joined on a scope:
    // an edge and the node it points at belong to the same graph, and two
    // members of an organization hold the same node id.
    `WITH seed AS (
       SELECT * FROM UNNEST($1::text[], $2::text[]) AS s(project, id)
     ),
     links AS (
       SELECT e.project, e.source_id AS seed_id, e.target_id AS node_id,
              e.relation_type, 'outgoing' AS direction
         FROM graph_edges AS e
         JOIN seed AS s ON s.project = e.project AND s.id = e.source_id
       UNION
       SELECT e.project, e.target_id AS seed_id, e.source_id AS node_id,
              e.relation_type, 'incoming' AS direction
         FROM graph_edges AS e
         JOIN seed AS s ON s.project = e.project AND s.id = e.target_id
     ),
     joined AS (
       SELECT l.project, l.seed_id, l.node_id, l.relation_type, l.direction,
              n.name, n.type, n.file_path, n.summary
         FROM links AS l
         JOIN graph_nodes AS n
           ON n.project = l.project AND n.id = l.node_id
        WHERE NOT starts_with(n.type, 'external_')
     ),
     capped AS (
       SELECT j.*, ROW_NUMBER() OVER (
                PARTITION BY j.project, j.seed_id
                -- A described node first: a summary is what makes an entry
                -- worth its tokens, and a hub file has more children than
                -- any packet can hold.
                ORDER BY (j.summary IS NULL), j.relation_type, j.node_id
              ) AS rn
         FROM joined AS j
     )
     SELECT c.project, p.type AS project_type, c.seed_id, c.node_id,
            c.relation_type, c.direction, c.name, c.type, c.file_path,
            c.summary, chunk.start_line, chunk.end_line,
            chunk.content_chunk AS chunk
       FROM capped AS c
       JOIN projects AS p ON p.name = c.project
       LEFT JOIN LATERAL (
         SELECT e.start_line, e.end_line, e.content_chunk
           FROM code_embeddings AS e
          WHERE e.project = c.project AND e.node_id = c.node_id
            AND e.kind = 'source'
          ORDER BY e.chunk_index
          LIMIT 1
       ) AS chunk ON TRUE
      WHERE c.rn <= $3
      -- rn, not the id: the caps downstream count in this order.
      ORDER BY c.project, c.seed_id, c.rn`,
    [
      frontier.map((seed) => seed.project),
      frontier.map((seed) => seed.id),
      NEIGHBOURS_PER_SEED,
    ],
  );
  return res.rows;
}

function seedCandidate(
  row: SearchRow,
  score: number,
  seedOrder: number,
): Candidate {
  return {
    key: `${row.project}\u0000${row.id}`,
    tier: null,
    chunk: row.snippet,
    seedOrder,
    place: 0,
    entry: {
      project: row.project,
      project_type: row.project_type,
      id: row.id,
      name: row.name,
      type: row.type,
      file_path: row.file_path,
      start_line: row.start_line,
      end_line: row.end_line,
      origin: "search",
      why:
        row.lexical_rank !== null && row.vector_rank !== null
          ? "lexical and semantic hit"
          : row.vector_rank !== null
            ? "semantic hit"
            : "lexical hit",
      score: Number(score.toFixed(5)),
      summary: row.summary,
    },
  };
}

/** Drop an entry whose lines are already covered by a kept entry. */
export function overlaps(kept: ContextEntry[], entry: ContextEntry): boolean {
  const start = entry.start_line;
  const end = entry.end_line;
  if (entry.file_path === null || start === null || end === null) {
    return false;
  }
  return kept.some((other) => {
    if (
      other.project !== entry.project ||
      other.file_path !== entry.file_path ||
      other.start_line === null ||
      other.end_line === null
    ) {
      return false;
    }
    return other.start_line <= end && start <= other.end_line;
  });
}

function cost(entry: ContextEntry): number {
  return estimateTokens(JSON.stringify(entry));
}

/**
 * Fill the budget: the search hits first, then the tiers in order. An
 * expanded node brings its summary; its chunk is an upgrade spent only once
 * everything that fits has been placed.
 */
export function assemble(
  seeds: Candidate[],
  expanded: Candidate[],
  budget: number,
  includeChunks: boolean,
  tierOrder: Tier[] = TIER_ORDER,
  upgradeChunks = true,
): { entries: ContextEntry[]; used: number; truncated: boolean } {
  const entries: ContextEntry[] = [];
  const upgradable: Candidate[] = [];
  let used = 0;
  let truncated = false;

  for (const candidate of seeds) {
    const entry = { ...candidate.entry };
    if (includeChunks && candidate.chunk !== null) {
      entry.chunk = candidate.chunk;
    }
    const price = cost(entry);
    if (used + price > budget) {
      // The reference alone is worth keeping when the text is not.
      const bare = { ...candidate.entry };
      if (used + cost(bare) > budget) {
        truncated = true;
        continue;
      }
      used += cost(bare);
      entries.push(bare);
      truncated = true;
      continue;
    }
    used += price;
    entries.push(entry);
  }

  for (const tier of tierOrder) {
    const inTier = expanded
      .filter((candidate) => candidate.tier === tier)
      .sort(
        (a, b) =>
          a.place - b.place ||
          a.seedOrder - b.seedOrder ||
          (a.entry.id < b.entry.id ? -1 : a.entry.id > b.entry.id ? 1 : 0),
      );
    for (const candidate of inTier) {
      if (overlaps(entries, candidate.entry)) {
        continue;
      }
      const price = cost(candidate.entry);
      if (used + price > budget) {
        truncated = true;
        continue;
      }
      used += price;
      entries.push(candidate.entry);
      if (includeChunks && upgradeChunks && candidate.chunk !== null) {
        upgradable.push(candidate);
      }
    }
  }

  for (const candidate of upgradable) {
    const entry = entries.find(
      (kept) =>
        kept.id === candidate.entry.id &&
        kept.project === candidate.entry.project,
    );
    if (entry === undefined || candidate.chunk === null) {
      continue;
    }
    const price = estimateTokens(candidate.chunk);
    if (used + price > budget) {
      truncated = true;
      break;
    }
    used += price;
    entry.chunk = candidate.chunk;
  }

  return { entries, used, truncated };
}

/**
 * Retrieve, expand, deduplicate and budget one question into a packet.
 *
 * Deterministic end to end: the reranked order decides the seeds, the tier
 * order decides the expansion, and nothing is sampled anywhere.
 */
export async function buildContext(
  pool: pg.Pool,
  ask: ContextRequest,
): Promise<ContextPacket> {
  const detail = resolveDetail(
    ask.detail ?? "auto",
    ask.query,
    ask.tokenBudget,
  );
  const found = await hybridSearch(pool, {
    named: ask.named,
    kind: ask.kind,
    query: ask.query,
    limit: ask.seeds,
    directories: detail === "summary",
  });
  const ranked = rerank(found.rows, ask.query, ask.seeds, ask.rerankEnabled);
  const seeds = ranked.map(({ row, score }, index) => {
    const seed = seedCandidate(row, score, index);
    // The summary is in the entry already, and a map keeps source for its best.
    if (
      row.matched === "summary" ||
      (detail === "summary" && index >= SUMMARY_CHUNK_SEEDS)
    ) {
      seed.chunk = null;
    }
    return seed;
  });

  const names = new Map<string, string>();
  for (const { row } of ranked) {
    names.set(`${row.project}\u0000${row.id}`, row.name);
  }

  const taken = new Set(seeds.map((seed) => seed.key));
  const expanded: Candidate[] = [];
  const relationships: ContextRelationship[] = [];
  // Which seed a node was reached from, and how much that seed has already
  // spent on the tier - the two numbers that keep one hub file from filling
  // the packet with its own table of contents.
  const order = new Map<string, number>(
    seeds.map((seed, at) => [seed.key, at]),
  );
  const tierSpend = new Map<string, number>();
  let frontier = ranked.map(({ row }) => ({
    project: row.project,
    id: row.id,
  }));
  const reached = new Map<string, Tier>();

  for (let hop = 1; hop <= MAX_HOPS && frontier.length > 0; hop += 1) {
    const rows = await fetchNeighbours(pool, frontier);
    const next: { project: string; id: string }[] = [];
    for (const row of rows) {
      const tier = classify(row.relation_type, row.direction, row.file_path);
      // A source packet leaves directories out; a summary one takes them
      // through the ancestor tier, which it spends first.
      if (tier === null || (tier === "container" && row.type === "directory")) {
        continue;
      }
      const seedKey = `${row.project}\u0000${row.seed_id}`;
      // A second hop stays in the tier that reached it: the caller of a
      // caller is still what calls this code, an import of a caller is not.
      const parent = reached.get(seedKey);
      if (hop > 1 && (parent === undefined || parent !== tier)) {
        continue;
      }
      if (hop > hopsFor(tier, ask.expand)) {
        continue;
      }
      const importer =
        tier === "caller" && isImporter(row.relation_type, row.direction);
      const group = `${seedKey}\u0000${importer ? "importer" : tier}`;
      const place = tierSpend.get(group) ?? 0;
      const seedOrder = order.get(seedKey) ?? seeds.length;
      if (place >= capFor(tier, seedOrder, importer)) {
        continue;
      }
      relationships.push({
        project: row.project,
        from: row.seed_id,
        relation: row.relation_type,
        to: row.node_id,
        direction: row.direction,
      });
      const key = `${row.project}\u0000${row.node_id}`;
      if (taken.has(key)) {
        continue;
      }
      taken.add(key);
      reached.set(key, tier);
      tierSpend.set(group, place + 1);
      order.set(key, seedOrder);
      names.set(key, row.name);
      expanded.push({
        key,
        tier,
        chunk: row.chunk,
        seedOrder,
        place,
        entry: {
          project: row.project,
          project_type: row.project_type,
          id: row.node_id,
          name: row.name,
          type: row.type,
          file_path: row.file_path,
          start_line: row.start_line ?? lineFromId(row.node_id),
          end_line: row.end_line,
          origin: "expansion",
          why: `${hop > 1 ? "indirect " : ""}${why(tier, row.relation_type, shorten(row.seed_id))}`,
          summary: row.summary,
        },
      });
      if (hop < hopsFor(tier, ask.expand)) {
        next.push({ project: row.project, id: row.node_id });
      }
    }
    frontier = next;
  }

  if (detail === "summary") {
    const ancestors = await fetchAncestors(
      pool,
      ranked.map(({ row }) => ({ project: row.project, id: row.id })),
    );
    for (const row of ancestors) {
      const key = `${row.project}\u0000${row.node_id}`;
      if (taken.has(key)) {
        continue;
      }
      taken.add(key);
      const seedOrder = order.get(`${row.project}\u0000${row.seed_id}`) ?? 0;
      expanded.push({
        key,
        tier: "ancestor",
        chunk: null,
        seedOrder,
        place: row.depth,
        entry: {
          project: row.project,
          project_type: row.project_type,
          id: row.node_id,
          name: row.name,
          type: row.type,
          file_path: row.file_path,
          start_line: lineFromId(row.node_id),
          end_line: null,
          origin: "expansion",
          why: why("ancestor", "contains", shorten(row.seed_id)),
          summary: row.summary,
        },
      });
    }
  }

  // A slice of the budget is held back for the links: a packet of entries
  // that never says how they connect is half an answer.
  const reserve = Math.floor(ask.tokenBudget / 8);
  const { entries, used, truncated } = assemble(
    seeds,
    expanded,
    Math.max(ask.tokenBudget - reserve, 0),
    ask.includeChunks,
    detail === "summary" ? SUMMARY_TIER_ORDER : TIER_ORDER,
    detail === "source",
  );

  // A relation to something the budget left out explains nothing, so the
  // links are cut to the entries that survived, and then to what is left.
  const kept = new Set(
    entries.map((entry) => `${entry.project ?? ""}\u0000${entry.id}`),
  );
  let spent = used;
  let linksCut = false;
  const links: ContextRelationship[] = [];
  const byWeight = [
    ...relationships.filter((link) => link.relation !== "contains"),
    ...relationships.filter((link) => link.relation === "contains"),
  ];
  for (const link of byWeight) {
    if (
      !kept.has(`${link.project ?? ""}\u0000${link.from}`) ||
      !kept.has(`${link.project ?? ""}\u0000${link.to}`)
    ) {
      continue;
    }
    const price = estimateTokens(JSON.stringify(link));
    if (spent + price > ask.tokenBudget) {
      linksCut = true;
      break;
    }
    spent += price;
    links.push(link);
  }

  // The project columns are noise when every row carries the same two
  // values, exactly as in search_code.
  const projects = [...new Set(entries.map((entry) => entry.project ?? ""))]
    .filter((name) => name !== "")
    .sort();
  if (ask.named !== null && projects.length <= 1) {
    for (const entry of entries) {
      delete entry.project;
      delete entry.project_type;
    }
    for (const link of links) {
      delete link.project;
    }
  }

  const notes: string[] = [];
  if (detail === "summary") {
    notes.push(
      "Summary packet: the entries are where the hits sit and what they " +
        "hold, with source for the best hits only. Ask with " +
        'detail: "source" for the code, or drill down with get_overview.',
    );
  }
  const semantic = semanticNote(found);
  if (semantic !== null) {
    notes.push(semantic);
  }
  if (entries.length === 0) {
    notes.push(
      "Nothing matched: no node of this scope answered the query. Ask it in " +
        'other words, widen the scope with project: "*", or check that ' +
        "the project has been indexed.",
    );
  } else if (
    ask.includeChunks &&
    detail === "source" &&
    entries.every((entry) => entry.chunk === undefined)
  ) {
    notes.push(
      "No source text in this packet: the projects it reached hold no " +
        "embedded chunks, so the entries are references and summaries. " +
        "Switch embedding on for the project in the dashboard settings to " +
        "get the code itself.",
    );
  }
  if (truncated || linksCut) {
    notes.push(
      "Budget spent: entries or relations were left out. Raise " +
        "token_budget, narrow the query, or switch a tier off through expand.",
    );
  }

  return {
    query: ask.query,
    detail,
    projects,
    budget: {
      limit: ask.tokenBudget,
      used: spent,
      truncated: truncated || linksCut,
    },
    entries,
    relationships: links,
    notes,
  };
}
