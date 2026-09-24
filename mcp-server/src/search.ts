import type pg from "pg";
import { identifiers, keep } from "./rerank.js";
import type { Candidate } from "./rerank.js";

// Where a search query becomes a vector. The MCP server holds no model and
// no mounts; the worker API holds both, and it is the one process that knows
// which embedding server a project is pointed at.
const WORKER_API_URL = (process.env.WORKER_API_URL ?? "").replace(/\/$/, "");
const WORKER_API_TOKEN = process.env.WORKER_API_TOKEN ?? "";
// A search must not wait on a model that is thinking about something else.
// Past this the semantic half is dropped and the lexical half answers alone.
const EMBED_TIMEOUT_MS = 5000;
// The same question is asked more than once in a session - a retry, a
// narrower project, a second tool call around the same words - and the vector
// for it does not change. Small on purpose: this is a cache, not a store.
const EMBED_CACHE_SIZE = 64;
const embedCache = new Map<string, number[]>();

/**
 * Embed one query string, or return null when nothing can.
 *
 * Never throws. A search that cannot reach a model is a search with half its
 * evidence, which is worth answering; an error here would instead lose the
 * lexical half as well.
 */
export async function embedQuery(
  query: string,
  project: string | null,
): Promise<number[] | null> {
  if (WORKER_API_URL === "" || WORKER_API_TOKEN === "") {
    return null;
  }
  const key = `${project ?? "*"}\u0000${query}`;
  const cached = embedCache.get(key);
  if (cached !== undefined) {
    return cached;
  }
  const stop = AbortSignal.timeout(EMBED_TIMEOUT_MS);
  try {
    const answer = await fetch(`${WORKER_API_URL}/embed`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${WORKER_API_TOKEN}`,
      },
      body: JSON.stringify({ text: query, project: project ?? "" }),
      signal: stop,
    });
    if (!answer.ok) {
      return null;
    }
    const body = (await answer.json()) as { embedding?: unknown };
    const vector = body.embedding;
    if (!Array.isArray(vector) || vector.length === 0) {
      return null;
    }
    const numbers = vector.map(Number);
    if (numbers.some((value) => !Number.isFinite(value))) {
      return null;
    }
    if (embedCache.size >= EMBED_CACHE_SIZE) {
      // Oldest first: Map keeps insertion order, and a cache this small does
      // not earn a recency list of its own.
      embedCache.delete(embedCache.keys().next().value as string);
    }
    embedCache.set(key, numbers);
    return numbers;
  } catch {
    return null;
  }
}

export type SearchRow = Candidate & {
  start_line: number | null;
  end_line: number | null;
  summary: string | null;
  snippet: string | null;
  /** Which kind of chunk matched: a node's summary, or a file's source. */
  matched?: "summary" | "source" | null;
};

/** Render a vector the way pgvector parses it. */
export function vectorLiteral(vector: number[]): string {
  return `[${vector.join(",")}]`;
}

export interface HybridQuery {
  /** One project, an organization, or null for the whole database. */
  named: string | null;
  /** Project type to narrow an unnamed search by, or null. */
  kind: string | null;
  query: string;
  /** How many rows the caller will keep; the pool gathered is deeper. */
  limit: number;
  /** Let directory nodes into the pool; only a summary packet wants them. */
  directories?: boolean;
}

export interface HybridResult {
  rows: SearchRow[];
  /** False when no embedding server answered, which the caller reports. */
  vectorAvailable: boolean;
  /** Whether any chunk in scope carries a vector at all. */
  embedded: boolean;
}

export interface LexicalTerms {
  /** One tsquery term per content word, or null to keep websearch syntax. */
  terms: string[] | null;
  /** ILIKE patterns for the identifier-shaped tokens of the query. */
  names: string[];
  /** The content words, stemmed, matched against directory path segments. */
  words: string[];
}

// Quotes, an upper-case OR or a leading minus mean the caller wrote
// websearch syntax on purpose, and it is honoured as written.
const OPERATORS = /"|\sOR\s|(^|\s)-\w/;

// Past this many matching chunks a term is common: it still scores, but it no
// longer brings chunks into the pool.
const DF_CAP = 2000;

const SUFFIXES = ["ing", "ies", "ied", "es", "ed", "s"];
const MIN_STEM = 4;

// The simple config does not stem, so `refunds` never met `refund`: a light
// stem searched as a prefix covers the plural and the tenses of a word.
export function stem(word: string): string {
  for (const suffix of SUFFIXES) {
    if (word.endsWith(suffix) && word.length - suffix.length >= MIN_STEM) {
      const root = word.slice(0, -suffix.length);
      const verb = suffix === "ing" || suffix === "ed";
      return verb && /([b-df-hj-np-tv-z])\1$/.test(root)
        ? root.slice(0, -1)
        : root;
    }
  }
  return word;
}

// The same split as lexical_words() in migration 0023, or the query and the
// index disagree about what a word is.
export function splitCamel(text: string): string {
  return text
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
}

// websearch_to_tsquery ANDs every word, so a question matched no chunk; an OR
// over the content words, ranked by ts_rank, degrades to the best matches.
export function lexicalTerms(query: string): LexicalTerms {
  const roots = [
    ...new Set(
      (splitCamel(query).match(/[A-Za-z0-9]+/g) ?? [])
        .map((word) => word.toLowerCase())
        .filter(keep)
        .map(stem),
    ),
  ];
  const words = new Set(
    roots.map((root) => (root.length >= MIN_STEM ? `${root}:*` : root)),
  );
  const names = [...identifiers(query)]
    .filter(([word, shaped]) => shaped && /^[a-z0-9_]+$/.test(word))
    .map(([word]) => `%${word}%`);
  const terms = OPERATORS.test(query) || words.size === 0 ? null : [...words];
  return { terms, names, words: roots };
}

/**
 * Gather the fused lexical and semantic candidate pool for one query.
 *
 * The rows come back in no useful order: `rerank` is what orders them. Both
 * `search_code` and `get_context` read this, so the retrieval stage has one
 * implementation rather than two that drift.
 */
export async function hybridSearch(
  pool: pg.Pool,
  ask: HybridQuery,
): Promise<HybridResult> {
  const pattern = `%${ask.query}%`;
  const terms = lexicalTerms(ask.query);
  // The two halves are gathered to this depth each and then fused, so a
  // result that both agree on outranks one that only the better half
  // found. Deeper than the limit on purpose: fusion is only meaningful
  // where the lists overlap.
  const depth = Math.max(ask.limit * 3, 50);
  // Several chunks of one file fold into one row, so chunks go deeper.
  const chunkDepth = depth * 4;
  const vector = await embedQuery(ask.query, ask.named);
  const literal = vector === null ? null : vectorLiteral(vector);

  // HNSW stops at ef_search rows before the scope filter runs; an
  // iterative scan keeps reading until the chunk depth is met in scope.
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SET LOCAL hnsw.iterative_scan = relaxed_order");
    const res = await client.query<SearchRow>(
      // Reciprocal rank fusion: each half contributes 1/(60 + rank), so
      // the lists are combined by agreement rather than by scores that
      // mean different things - a cosine distance and a trigram
      // similarity are not comparable numbers.
      `WITH scope AS (
             SELECT p.name, p.type FROM projects AS p
              WHERE ($1::text IS NULL
                     OR p.name = $1
                     OR EXISTS (
                          SELECT 1 FROM project_members AS m
                           WHERE m.organization = $1 AND m.project = p.name
                        ))
                AND ($2::text IS NULL OR p.type = $2)
           ),
           ask AS (
             SELECT CASE WHEN $8::text[] IS NULL
                         THEN websearch_to_tsquery('simple', $4)
                         ELSE to_tsquery('simple', array_to_string($8, ' | '))
                    END AS tsq
           ),
           lex_nodes AS (
             SELECT n.project, n.id,
                    GREATEST(
                      similarity(n.name, $4),
                      similarity(n.id, $4),
                      CASE WHEN n.name ILIKE $3 OR n.id ILIKE $3
                           THEN 0.5 ELSE 0 END,
                      CASE WHEN n.name ILIKE ANY($9::text[])
                           THEN 0.4 ELSE 0 END,
                      CASE WHEN n.type IN ('file', 'directory')
                             OR n.metadata ->> 'summary_source'
                                IN ('llm', 'manual')
                           THEN ts_rank(
                             lexical_words(COALESCE(n.summary, '')), ask.tsq
                           )
                           ELSE 0 END
                    ) AS score,
                    NULL::int AS start_line, NULL::int AS end_line,
                    NULL::text AS snippet, NULL::text AS kind
               FROM graph_nodes AS n
               JOIN scope AS s ON s.name = n.project
               CROSS JOIN ask
              WHERE (n.name ILIKE $3 OR n.id ILIKE $3
                 OR n.name % $4 OR n.id % $4
                 OR n.name ILIKE ANY($9::text[])
                 OR (lexical_words(COALESCE(n.summary, '')) @@ ask.tsq
                     -- A symbol's auto summary is its docstring, already in
                     -- the chunk it sits in; matching it twice outranks files.
                     AND (n.type IN ('file', 'directory')
                          OR n.metadata ->> 'summary_source' IN ('llm', 'manual'))))
                AND ($11::boolean OR n.type <> 'directory')
              ORDER BY score DESC, n.id
              LIMIT $6
           ),
           -- A directory is named by its path, and a question names it by a
           -- longer word: "auth/" answers "authentication".
           lex_dirs AS (
             SELECT n.project, n.id, 0.5::real AS score,
                    NULL::int AS start_line, NULL::int AS end_line,
                    NULL::text AS snippet, NULL::text AS kind
               FROM graph_nodes AS n
               JOIN scope AS s ON s.name = n.project
              WHERE $11::boolean AND n.type = 'directory'
                AND EXISTS (
                      SELECT 1
                        FROM unnest(string_to_array(rtrim(n.id, '/'), '/'))
                               AS seg (part)
                        JOIN unnest($12::text[]) AS w (word)
                          ON length(seg.part) >= 3
                         AND (w.word LIKE lower(seg.part) || '%'
                              OR lower(seg.part) LIKE w.word || '%')
                    )
              LIMIT $6
           ),
           terms AS (
             SELECT t.term, to_tsquery('simple', t.term) AS q
               FROM unnest(COALESCE($8::text[], ARRAY[]::text[])) AS t (term)
           ),
           -- Document frequency, counted no further than the cap: a term at
           -- the cap is common, and its matches are never all read.
           df AS (
             SELECT t.term, t.q,
                    (SELECT COUNT(*)
                       FROM (SELECT 1
                               FROM code_embeddings AS e
                               JOIN scope AS s ON s.name = e.project
                              WHERE lexical_words(e.content_chunk) @@ t.q
                              LIMIT $10::int) AS hit
                    ) AS df
               FROM terms AS t
           ),
           weights AS (
             SELECT term, q, LN(1 + $10::float8 / df) AS idf,
                    df >= $10::int AS common
               FROM df
              WHERE df > 0
           ),
           total AS (SELECT SUM(idf) AS idf FROM weights),
           -- The pool comes from the rare terms, whose matches are all read;
           -- with none, every term is common and ts_rank orders the OR.
           pick AS (
             SELECT COALESCE(
                      (SELECT to_tsquery('simple', string_agg(term, ' | '))
                         FROM weights
                        WHERE NOT common),
                      ask.tsq
                    ) AS q
               FROM ask
           ),
           -- Materialized so each chunk's words are computed once, not once
           -- per term the score below tests them against.
           lex_pool AS MATERIALIZED (
             SELECT e.project, e.node_id AS id, e.start_line, e.end_line,
                    e.content_chunk AS snippet, e.kind,
                    lexical_words(e.content_chunk) AS words
               FROM code_embeddings AS e
               JOIN scope AS s ON s.name = e.project
               CROSS JOIN pick
              WHERE lexical_words(e.content_chunk) @@ pick.q
                AND ($11::boolean OR right(e.node_id, 1) <> '/')
           ),
           lex_chunks AS (
             SELECT project, id, score, start_line, end_line, snippet, kind
               FROM (
                 SELECT c.project, c.id, c.start_line, c.end_line, c.snippet,
                        c.kind,
                        COALESCE(
                          (SELECT SUM(w.idf) FROM weights AS w
                            WHERE c.words @@ w.q) / NULLIF(total.idf, 0),
                          ts_rank(c.words, ask.tsq)
                        ) AS score,
                        ts_rank(c.words, ask.tsq) AS tie
                   FROM lex_pool AS c
                   CROSS JOIN ask
                   CROSS JOIN total
               ) AS scored
              ORDER BY score DESC, tie DESC, id
              LIMIT $6
           ),
           lexical AS (
             SELECT DISTINCT ON (project, id)
                    project, id, score, start_line, end_line, snippet, kind
               FROM (
                 SELECT * FROM lex_nodes
                 UNION ALL
                 SELECT * FROM lex_dirs
                 UNION ALL
                 SELECT * FROM lex_chunks
               ) AS lexical_all
              ORDER BY project, id, score DESC
           ),
           lexical_ranked AS (
             SELECT project, id, start_line, end_line, snippet, kind,
                    ROW_NUMBER() OVER (ORDER BY score DESC, id) AS rank
               FROM lexical
           ),
           vector_hits AS (
             SELECT e.project, e.node_id AS id,
                    1 - (e.embedding <=> $5::vector) AS score,
                    e.start_line, e.end_line, e.content_chunk AS snippet, e.kind
               FROM code_embeddings AS e
               JOIN scope AS s ON s.name = e.project
              WHERE $5::text IS NOT NULL AND e.embedding IS NOT NULL
                AND ($11::boolean OR right(e.node_id, 1) <> '/')
              ORDER BY e.embedding <=> $5::vector
              LIMIT $7
           ),
           vector_ranked AS (
             SELECT project, id, start_line, end_line, snippet, kind,
                    ROW_NUMBER() OVER (ORDER BY score DESC, id) AS rank
               FROM (
                 SELECT DISTINCT ON (project, id)
                        project, id, score, start_line, end_line, snippet, kind
                   FROM vector_hits
                  ORDER BY project, id, score DESC
               ) AS best
           ),
           fused AS (
             SELECT COALESCE(l.project, v.project) AS project,
                    COALESCE(l.id, v.id) AS id,
                    COALESCE(1.0 / (60 + l.rank), 0)
                      + COALESCE(1.0 / (60 + v.rank), 0) AS score,
                    COALESCE(v.start_line, l.start_line) AS start_line,
                    COALESCE(v.end_line, l.end_line) AS end_line,
                    COALESCE(v.snippet, l.snippet) AS snippet,
                    COALESCE(v.kind, l.kind) AS kind,
                    l.rank AS lexical_rank, v.rank AS vector_rank
               FROM lexical_ranked AS l
               FULL OUTER JOIN vector_ranked AS v
                 ON v.project = l.project AND v.id = l.id
           ),
           ranked AS (
             SELECT f.*, ROW_NUMBER() OVER (
                      PARTITION BY f.project ORDER BY f.score DESC, f.id
                    ) AS rn
               FROM fused AS f
           )
           SELECT n.project, s.type AS project_type, n.id, n.name, n.type,
                  n.file_path, NULLIF(r.start_line, 0) AS start_line,
                  NULLIF(r.end_line, 0) AS end_line, r.kind AS matched,
                  r.score::float8 AS rrf,
                  r.lexical_rank::int AS lexical_rank,
                  r.vector_rank::int AS vector_rank, n.summary,
                  LEFT(r.snippet, 400) AS snippet,
                  (SELECT COUNT(*)::int
                     FROM graph_edges AS g
                     JOIN graph_nodes AS src
                       ON src.project = g.project AND src.id = g.source_id
                    WHERE g.project = n.project AND g.target_id = n.id
                      AND g.relation_type <> 'contains'
                      AND NOT starts_with(src.type, 'external_')
                  ) AS in_degree
             FROM ranked AS r
             JOIN graph_nodes AS n
               ON n.project = r.project AND n.id = r.id
             JOIN scope AS s ON s.name = n.project
            WHERE r.rn <= $6`,
      [
        ask.named,
        ask.kind,
        pattern,
        ask.query,
        literal,
        depth,
        chunkDepth,
        terms.terms,
        terms.names,
        DF_CAP,
        ask.directories ?? false,
        terms.words,
      ],
    );
    let embedded = res.rows.some((row) => row.vector_rank !== null);
    if (!embedded && vector !== null) {
      const probe = await client.query<{ embedded: boolean }>(
        `SELECT EXISTS (
                  SELECT 1 FROM code_embeddings AS e
                    JOIN projects AS p ON p.name = e.project
                   WHERE e.embedding IS NOT NULL
                     AND ($1::text IS NULL
                          OR p.name = $1
                          OR EXISTS (
                               SELECT 1 FROM project_members AS m
                                WHERE m.organization = $1 AND m.project = p.name
                             ))
                     AND ($2::text IS NULL OR p.type = $2)
                ) AS embedded`,
        [ask.named, ask.kind],
      );
      embedded = probe.rows[0]?.embedded ?? false;
    }
    await client.query("COMMIT");
    return { rows: res.rows, vectorAvailable: vector !== null, embedded };
  } catch (error) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw error;
  } finally {
    client.release();
  }
}

/**
 * Say which halves answered, or null when both did.
 *
 * Not decoration: a lexical-only answer to a question asked in words is a
 * weaker answer, and the caller has no other way to tell that is what it got.
 */
export function semanticNote(result: HybridResult): string | null {
  if (!result.vectorAvailable) {
    return (
      "Semantic half unavailable: no embedding server answered, so " +
      "these are lexical matches only. `search_code` gains the " +
      "vector half once embedding is switched on for the project in " +
      "the dashboard settings and its queue has drained."
    );
  }
  if (result.rows.some((row) => row.vector_rank !== null)) {
    return null;
  }
  if (result.embedded) {
    return (
      "Semantic half matched nothing in scope, so these are lexical " +
      "matches only."
    );
  }
  return (
    "Semantic half returned nothing: nothing in scope has embeddings " +
    "yet, so these are lexical matches only."
  );
}
