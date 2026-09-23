/** One fused search_code row, with the graph features the reranker reads. */
export interface Candidate {
  project: string;
  project_type: string;
  id: string;
  name: string;
  type: string;
  file_path: string | null;
  rrf: number;
  lexical_rank: number | null;
  vector_rank: number | null;
  in_degree: number;
}

export interface Ranked<T extends Candidate> {
  row: T;
  score: number;
}

// Every weight is a fraction of a top-ranked hit in one half, 1/(60 + 1),
// so a bonus of 0.25 is worth as much as a quarter of that hit.
const UNIT = 1 / 61;
const IDENTIFIER_BONUS = 0.75;
const WORD_NAME_BONUS = 0.25;
const PATH_TOKEN_BONUS = 0.1;
const PATH_TOKEN_CAP = 3;
const POOL_LINK_BONUS = 0.05;
const POOL_LINK_CAP = 3;
const DEGREE_BONUS = 0.03;
const DEGREE_CAP = 3;
const PROSE_PENALTY = -0.25;
const EXTERNAL_PENALTY = -0.5;

const PROSE_TYPES = new Set(["heading", "image"]);
const PROSE_PROJECTS = new Set(["docs", "memory", "suggestions"]);
const STOPWORDS = new Set([
  "and",
  "are",
  "does",
  "for",
  "from",
  "how",
  "into",
  "the",
  "that",
  "this",
  "what",
  "when",
  "where",
  "which",
  "who",
  "why",
  "with",
]);

export function keep(word: string): boolean {
  return word.length >= 3 && !STOPWORDS.has(word);
}

// An identifier-shaped token (readLimit, queue_embeddings) names a symbol; a
// plain word that happens to equal a name is weaker evidence.
export function identifiers(text: string): Map<string, boolean> {
  const found = new Map<string, boolean>();
  for (const word of text.match(/[A-Za-z0-9_$]+/g) ?? []) {
    const lower = word.toLowerCase();
    if (keep(lower)) {
      const shaped = /[_$0-9]|[a-z][A-Z]/.test(word);
      found.set(lower, (found.get(lower) ?? false) || shaped);
    }
  }
  return found;
}

function subwords(text: string): Set<string> {
  const split = text
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[^A-Za-z0-9]+/)
    .map((word) => word.toLowerCase());
  return new Set(split.filter(keep));
}

function withoutExtension(path: string): string {
  return path.replace(/\.[A-Za-z0-9]+$/, "");
}

function bareName(name: string): string {
  return name.replace(/\(\)$/, "").replace(/^\./, "").toLowerCase();
}

function isProse(row: Candidate): boolean {
  return PROSE_TYPES.has(row.type) && !PROSE_PROJECTS.has(row.project_type);
}

function isWeak(row: Candidate): boolean {
  return row.type.startsWith("external_") || isProse(row);
}

function compareIds(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function poolLinks(rows: Candidate[]): Map<Candidate, number> {
  const perFile = new Map<string, number>();
  const key = (row: Candidate) => `${row.project}\u0000${row.file_path}`;
  for (const row of rows) {
    if (row.file_path !== null && !isWeak(row)) {
      perFile.set(key(row), (perFile.get(key(row)) ?? 0) + 1);
    }
  }
  const links = new Map<Candidate, number>();
  for (const row of rows) {
    const same = row.file_path === null ? 0 : (perFile.get(key(row)) ?? 0);
    links.set(row, Math.max(same - (isWeak(row) ? 0 : 1), 0));
  }
  return links;
}

function bonus(
  row: Candidate,
  links: number,
  queryIdentifiers: Map<string, boolean>,
  queryWords: Set<string>,
): number {
  let units = 0;

  const shaped = queryIdentifiers.get(bareName(row.name));
  if (shaped !== undefined) {
    units += shaped ? IDENTIFIER_BONUS : WORD_NAME_BONUS;
  }

  if (row.file_path !== null) {
    const segments = subwords(withoutExtension(row.file_path));
    const hits = [...queryWords].filter((word) => segments.has(word)).length;
    units += Math.min(hits, PATH_TOKEN_CAP) * PATH_TOKEN_BONUS;
  }

  if (row.type.startsWith("external_")) {
    units += EXTERNAL_PENALTY;
  } else if (isProse(row)) {
    units += PROSE_PENALTY;
  }

  units += Math.min(links, POOL_LINK_CAP) * POOL_LINK_BONUS;
  units += Math.min(Math.log1p(row.in_degree), DEGREE_CAP) * DEGREE_BONUS;

  return units * UNIT;
}

/**
 * Order fused candidates and cut them to `limit`, interleaving projects so
 * each one's best row comes before any project's second. With `enabled`
 * false the score is the fused RRF alone.
 */
export function rerank<T extends Candidate>(
  rows: T[],
  query: string,
  limit: number,
  enabled: boolean,
): Ranked<T>[] {
  const queryIdentifiers = identifiers(query);
  const queryWords = subwords(query);
  const links = poolLinks(rows);

  const scored = rows.map((row) => ({
    row,
    score: enabled
      ? row.rrf + bonus(row, links.get(row) ?? 0, queryIdentifiers, queryWords)
      : row.rrf,
  }));
  scored.sort((a, b) => b.score - a.score || compareIds(a.row.id, b.row.id));

  const seen = new Map<string, number>();
  const placed = scored.map((item) => {
    const rank = (seen.get(item.row.project) ?? 0) + 1;
    seen.set(item.row.project, rank);
    return { item, rank };
  });
  placed.sort(
    (a, b) =>
      a.rank - b.rank ||
      b.item.score - a.item.score ||
      compareIds(a.item.row.id, b.item.row.id),
  );

  return placed.slice(0, limit).map(({ item }) => item);
}
