"""Gather the fused lexical and semantic pool one query is answered from."""

from __future__ import annotations

import json
import math
import re
import urllib.request
from typing import Any

from enggraph.mcp import db, jsjson
from enggraph.mcp.rerank import identifiers, keep
from enggraph.mcp.worker import WORKER_API_TOKEN, WORKER_API_URL, read_ranges

SNIPPET_CHARS = 400
# A search must not wait on a model that is thinking about something else.
# Past this the semantic half is dropped and the lexical half answers alone.
EMBED_TIMEOUT = 5
EMBED_CACHE_SIZE = 64
_embed_cache: dict[tuple[str, str], list[float]] = {}


def embed_query(query: str, project: str | None) -> list[float] | None:
    """Embed one query string, or return None when nothing can.

    Never raises: a search that cannot reach a model is a search with half
    its evidence, and an error here would lose the lexical half as well.
    """
    if WORKER_API_URL == "" or WORKER_API_TOKEN == "":
        return None
    key = (project if project is not None else "*", query)
    cached = _embed_cache.get(key)
    if cached is not None:
        return cached
    request = urllib.request.Request(  # noqa: S310
        f"{WORKER_API_URL}/embed",
        data=json.dumps({"text": query, "project": project or ""}).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {WORKER_API_TOKEN}",
        },
    )
    try:
        opened = urllib.request.urlopen(request, timeout=EMBED_TIMEOUT)  # noqa: S310
        with opened as answer:
            body = json.loads(answer.read().decode("utf-8"))
        vector = body.get("embedding") if isinstance(body, dict) else None
        if not isinstance(vector, list) or not vector:
            return None
        numbers = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in numbers):
            return None
    except Exception:  # noqa: BLE001 - the lexical half still answers
        return None
    if len(_embed_cache) >= EMBED_CACHE_SIZE:
        _embed_cache.pop(next(iter(_embed_cache)), None)
    _embed_cache[key] = numbers
    return numbers


def vector_literal(vector: list[float]) -> str:
    """Render a vector the way pgvector parses it."""
    return "[" + ",".join(jsjson.number(float(value)) for value in vector) + "]"


# Quotes, an upper-case OR or a leading minus mean the caller wrote
# websearch syntax on purpose, and it is honoured as written.
OPERATORS = re.compile(r'"|\sOR\s|(^|\s)-[A-Za-z0-9_]')

# Past this many matching chunks a term is common: it still scores, but it no
# longer brings chunks into the pool.
DF_CAP = 2000

SUFFIXES = ["ing", "ies", "ied", "es", "ed", "s"]
MIN_STEM = 4
DOUBLED = re.compile(r"([b-df-hj-np-tv-z])\1\Z")
PLAIN_NAME = re.compile(r"\A[a-z0-9_]+\Z")


def stem(word: str) -> str:
    """Cut a plural or a tense off a word; the index does not stem either."""
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= MIN_STEM:
            root = word[: -len(suffix)]
            verb = suffix in ("ing", "ed")
            return root[:-1] if verb and DOUBLED.search(root) else root
    return word


def split_camel(text: str) -> str:
    """Split camelCase the way lexical_words() does in the database."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)


def lexical_terms(query: str) -> dict[str, Any]:
    """Return the tsquery terms, name patterns and stemmed words of a query.

    websearch_to_tsquery ANDs every word, so a question matched no chunk; an
    OR over the content words, ranked, degrades to the best matches.
    """
    found = [word.lower() for word in re.findall(r"[A-Za-z0-9]+", split_camel(query))]
    roots = list(dict.fromkeys(stem(word) for word in found if keep(word)))
    words = list(
        dict.fromkeys(f"{root}:*" if len(root) >= MIN_STEM else root for root in roots)
    )
    names = [
        f"%{word}%"
        for word, shaped in identifiers(query).items()
        if shaped and PLAIN_NAME.match(word)
    ]
    terms = None if OPERATORS.search(query) or not words else words
    return {"terms": terms, "names": names, "words": roots}


# Reciprocal rank fusion: each half contributes 1/(60 + rank), so the lists
# are combined by agreement rather than by scores that mean different things.
HYBRID = """WITH scope AS (
             SELECT p.name, p.type FROM projects AS p
              WHERE ($1::text IS NULL
                     OR p.name = $1
                     OR EXISTS (
                          SELECT 1 FROM org_members AS m
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
               FROM nodes AS n
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
               FROM nodes AS n
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
                               FROM chunks AS e
                               JOIN scope AS s ON s.name = e.project
                              WHERE e.words @@ t.q
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
                    NULL::text AS snippet, e.kind, e.words
               FROM chunks AS e
               JOIN scope AS s ON s.name = e.project
               CROSS JOIN pick
              WHERE e.words @@ pick.q
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
                    e.start_line, e.end_line, NULL::text AS snippet, e.kind
               FROM chunks AS e
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
                  r.snippet,
                  (SELECT COUNT(*)::int
                     FROM edges AS g
                     JOIN nodes AS src
                       ON src.project = g.project AND src.id = g.source_id
                    WHERE g.project = n.project AND g.target_id = n.id
                      AND g.relation_type <> 'contains'
                      AND NOT starts_with(src.type, 'external_')
                  ) AS in_degree
             FROM ranked AS r
             JOIN nodes AS n
               ON n.project = r.project AND n.id = r.id
             JOIN scope AS s ON s.name = n.project
            WHERE r.rn <= $6"""

EMBEDDED_PROBE = """SELECT EXISTS (
                  SELECT 1 FROM chunks AS e
                    JOIN projects AS p ON p.name = e.project
                   WHERE e.embedding IS NOT NULL
                     AND ($1::text IS NULL
                          OR p.name = $1
                          OR EXISTS (
                               SELECT 1 FROM org_members AS m
                                WHERE m.organization = $1 AND m.project = p.name
                             ))
                     AND ($2::text IS NULL OR p.type = $2)
                ) AS embedded"""


def hybrid_search(
    named: str | None,
    kind: str | None,
    query: str,
    limit: int,
    directories: bool = False,
) -> dict[str, Any]:
    """Gather the fused lexical and semantic candidate pool for one query.

    The rows come back in no useful order: `rerank` is what orders them.
    """
    pattern = f"%{query}%"
    terms = lexical_terms(query)
    # Both halves are gathered deeper than the limit: fusion is only
    # meaningful where the lists overlap.
    depth = max(limit * 3, 50)
    # Several chunks of one file fold into one row, so chunks go deeper.
    chunk_depth = depth * 4
    vector = embed_query(query, named)
    literal = None if vector is None else vector_literal(vector)

    # HNSW stops at ef_search rows before the scope filter runs; an
    # iterative scan keeps reading until the chunk depth is met in scope.
    with db.transaction() as client:
        client.query("SET LOCAL hnsw.iterative_scan = relaxed_order")
        rows = client.query(
            HYBRID,
            [
                named,
                kind,
                pattern,
                query,
                literal,
                depth,
                chunk_depth,
                terms["terms"],
                terms["names"],
                DF_CAP,
                directories,
                terms["words"],
            ],
        )
        embedded = any(row["vector_rank"] is not None for row in rows)
        if not embedded and vector is not None:
            probe = client.query(EMBEDDED_PROBE, [named, kind])
            embedded = bool(probe[0]["embedded"]) if probe else False
    return {
        "rows": with_snippets(rows),
        "vectorAvailable": vector is not None,
        "embedded": embedded,
    }


def with_snippets(rows: list[db.Row]) -> list[db.Row]:
    """Show what matched: a summary, or source read from the mounted tree."""
    wanted = [
        {
            "project": row["project"],
            "path": row["file_path"],
            "start": row["start_line"],
            "end": row["end_line"],
        }
        if row.get("matched") == "source"
        and row["file_path"] is not None
        and row["start_line"] is not None
        and row["end_line"] is not None
        else None
        for row in rows
    ]
    texts = iter(read_ranges([one for one in wanted if one is not None]))
    shown = []
    for row, one in zip(rows, wanted, strict=True):
        if one is not None:
            text = next(texts)
            snippet = None if text is None else text[:SNIPPET_CHARS]
        elif row.get("matched") == "summary":
            snippet = (row["summary"] or "")[:SNIPPET_CHARS] or None
        else:
            snippet = None
        shown.append({**row, "snippet": snippet})
    return shown


def semantic_note(result: dict[str, Any]) -> str | None:
    """Say which halves answered, or None when both did.

    A lexical-only answer to a question asked in words is a weaker answer,
    and the caller has no other way to tell that is what it got.
    """
    if not result["vectorAvailable"]:
        return (
            "Semantic half unavailable: no embedding server answered, so "
            "these are lexical matches only. `search_code` gains the "
            "vector half once embedding is switched on for the project in "
            "the dashboard settings and its queue has drained."
        )
    if any(row["vector_rank"] is not None for row in result["rows"]):
        return None
    if result["embedded"]:
        return (
            "Semantic half matched nothing in scope, so these are lexical matches only."
        )
    return (
        "Semantic half returned nothing: nothing in scope has embeddings "
        "yet, so these are lexical matches only."
    )
