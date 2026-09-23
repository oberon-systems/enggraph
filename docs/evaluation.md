---
layout: default
title: Evaluation
nav_order: 9
---

## What it checks

Lint and type checks do not catch a query that PostgreSQL rejects, a queue
that never drains, or a reranker change that makes half the answers worse.
The evaluation harness checks each of those against a real database:

| Layer     | Where                                      | Needs a stack |
| --------- | ------------------------------------------ | ------------- |
| unit      | `mcp-server/test/{rerank,context,symbols}` | no            |
| SQL       | `mcp-server/test/sql.test.ts`              | database      |
| SQL       | `graphify/tests/test_sql_db.py`            | database      |
| benchmark | `mcp-server/eval/run.ts`                   | database      |
| MCP tools | `mcp-server/test/e2e.test.ts`              | MCP server    |

The SQL layers collect every SQL literal in the sources and `PREPARE` it
against the migrated schema, so a new query is covered without being
registered anywhere. The graphify layer also drives the embedding queue
through enqueue, claim, finish and fail inside a rolled-back transaction.

## The eval stack

`docker-compose.eval.yaml` is a separate compose project, `enggraph-eval`. Its
database lives on tmpfs, and it binds only loopback ports: `55432` for
PostgreSQL and `53000` for MCP. `make eval-up` migrates it and indexes
`eval/corpus/alpha` as project `alpha`. It never touches the main stack.

The corpus is a small synthetic service: TypeScript auth and payments code,
a Python worker, tests on both sides, a Dockerfile and a compose file. Every
name in it is neutral.

## Running it

Every test suite, in one call:

```bash
make test
```

It runs `test-mcp` (a typecheck first) and `test-graphify`, the suites that
need no stack. Then `test-eval` builds the graphify and MCP images from the
working tree, brings up the eval stack on them, runs the benchmark against
the baseline and then the SQL and MCP tool tests, and removes the stack
whether they pass or not. Each target also
runs alone, and `ARGS` reaches vitest or pytest:

```bash
make test-mcp ARGS=test/symbols.test.ts
make test-graphify ARGS="-k chunks"
make test-eval
```

To iterate on the benchmark, keep a stack up between runs. `eval-up` starts
the images as they are, so build them first when the code changed:

```bash
make build
make eval-up
make eval
make eval-down
```

`make eval` runs the benchmark first, then `eval-checks`, which is what
`test-eval` runs on its own stack. Pass benchmark flags through `ARGS`:

```bash
make eval ARGS=--rerank=false
make eval ARGS=--update-baseline
make eval ARGS=--tolerance=0.05
```

## Reading the benchmark

Each query in `eval/queries.yaml` names the files a good answer must reach.
The benchmark scores the reranked `search_code` rows and the `get_context`
packet for every query:

| Metric              | Meaning                                                 |
| ------------------- | ------------------------------------------------------- |
| `recall_at_5`, `10` | share of expected files in the top 5 or 10 search files |
| `mrr`               | 1 / rank of the first expected file the search returns  |
| `context_recall`    | share of expected files present in the packet           |
| `context_precision` | share of packet entries that sit in an expected file    |
| `tokens_mean`       | tokens the packet spent                                 |
| `*_ms_p50`, `p95`   | latency of the search and of the packet                 |

The results go to `eval/results/<mode>.json`, which git ignores. The mode is
`lexical` when no embedding server answered and `hybrid` when one did, with
`-norerank` appended for `--rerank=false`. Each mode has its own baseline in
`eval/baseline.<mode>.json`.

A `callers`, `tests` or `impact` query that names a `symbol` is also asked of
`find_callers`, `find_tests` or `impact_analysis`. The files of the answer,
the definition first, are scored as `recall_at_5`, `recall` and `mrr` under
`by_tool`, beside the search score for the same query. `by_tool` is reported,
not gated.

## The baseline gate

The run fails when any quality metric falls more than the tolerance (0.02 by
default) below the checked-in baseline. Latency and tokens are reported but
never gated. An improvement does not update the baseline on its own: record
it on purpose with `--update-baseline` and commit the file with the change
that earned it.

## Adding a query

Add an entry to `eval/queries.yaml` with an `id`, a `kind` (`discovery`,
`config`, `symbol`, `callers`, `tests` or `impact`), the `query` and the
`expect` list. A `callers`, `tests` or `impact` query takes a `symbol` as
well, which is what the matching tool is asked about. If the answer needs code the corpus lacks, add it to
`eval/corpus/alpha` first. Then run the benchmark and update the baseline,
because a new query moves every mean.

## Where the code is

- `mcp-server/eval/run.ts` - the benchmark runner.
- `mcp-server/test/` - the unit, SQL and MCP tool tests (vitest).
- `graphify/tests/test_sql_db.py` - the indexer's SQL and queue, marker `db`.
- `docker-compose.eval.yaml` - the throwaway stack.
- `.github/workflows/test.yml` - CI: unit tests on every push, then the eval
  stack built from the commit's own images.
