---
layout: default
title: BREAKING CHANGES
nav_order: 10
---

## BREAKING CHANGES

What an upgrade changes that a running install has to act on, newest first.
Take a whole-database backup before every upgrade
([Upgrading](deployment.html#upgrading)): a migration is undone only by
restoring that backup.

## The dashboard is a Python package

Releases after 0.24.0 run the dashboard from `packages/web`: FastAPI, Jinja2
templates and htmx. Its addresses, its `/api` and what each page does are
unchanged.

### What changed

- **Node.js is not needed** to build, run or develop any part of the stack.
- **Image.** `ghcr.io/oberon-systems/enggraph/web` keeps its name and is built
  from `packages/web/Dockerfile` with the repository root as its context.
- **Removed.** The `web/` directory, `make web <target>`, the `web-check`
  pre-commit hook and `scripts/web-check.sh`.
- **Pages are rendered on the server.** A link opens a page instead of
  redrawing one; lists, lamps and queues refresh themselves as before.
- **An address no page lives at** answers 404 with the same text. It
  answered 200.
- **Markdown** in plans, memories and suggestions is rendered on the server,
  so the markup of an unusual document may differ in details.

### Upgrading

```bash
make backup
git pull
make init        # installs the new package into .venv
make build       # or: make pull
make up
make status
```

## The MCP server is a Python package

Releases after 0.24.0 run the MCP server from `packages/mcp`. Its addresses,
the 41 tools, their descriptions, schemas and answers are unchanged.

### What changed

- **Image.** `ghcr.io/oberon-systems/enggraph/mcp-server` is no longer built.
  The compose service keeps the name `mcp-server` and runs the `mcp` image.
- **Removed.** The `mcp-server/` directory, `make mcp <target>` and
  `make test-mcp`. The server's tests run with `make test-py`.
- **Older SSE clients.** The address the `/sse` stream announces for messages
  carries `session_id`, not `sessionId`. A client that posts to the address
  it was sent is unaffected.
- **Tool results** carry `isError: false` where the field was left out.

### Upgrading

```bash
make backup
git pull
make build       # or: make pull
make up
make status
```

`make status` shows `mcp-server` healthy. The old image can be removed:

```bash
docker image rm ghcr.io/oberon-systems/enggraph/mcp-server:latest
```

## Alembic owns the schema, goose is gone

Releases after 0.24.0 apply the schema with Alembic. The schema itself does
not change: revision `0001` creates exactly what goose left after its
migration 31.

### What changed

- **The `migrate` service** runs `python -m enggraph.core.migrate` from the
  `api` image. The goose image and the `migrations/` directory are removed.
- **A database at goose 31** is marked as revision `0001` on the first
  `make up`. No table is created, changed or read.
- **A database below goose 31 is refused**, and no service starts. The steps
  from there to 31 exist only in 0.24.0, the last release that carries goose.
- **`make db`** keeps its targets. `make db new` writes a Python revision,
  and a new revision is applied only after `make build`.
- **`make status`** reads the revision from `alembic_version`. The
  `schema_migrations` table stays in the database and is no longer written.

### Upgrading

```bash
make db version  # on the old release: the last line must name 0031
make backup
git pull
make build       # or: make pull
make up
docker compose logs --tail 5 migrate
```

The `migrate` log says `Schema is at goose 31: marking it as 0001`, once.

If `make db version` shows less than 31, check out `v0.24.0` first, run
`make build` and `make up` there, and only then upgrade to this one.
The entry on migration 0031 below describes that step.

A backup taken below goose 31 cannot be restored into this release either:
restore it under the release that wrote it, upgrade to 0.24.0, then to this
one.

## Python services in `packages/`, queues as services

Releases after 0.23.0 split the `graphify` image into one image per service.
Tool names, MCP addresses, the database schema and `.env` settings are
unchanged.

### What changed

- **Images.** `ghcr.io/oberon-systems/enggraph/graphify` is no longer built.
  The stack runs `api` (the worker API and the index job), `embed`,
  `summarize` and `viewer`.
- **Two new services.** `embed` drains the embedding queue and `summarize`
  pushes the summary queue. Both used to be threads inside `worker-api`.
  Their limits are `EMBEDQ_CPUS`, `EMBEDQ_MEM`, `SUMMARIZE_CPUS` and
  `SUMMARIZE_MEM`; `API_CPUS` gets a smaller default share.
- **Removed make targets.** `make summarize`, `make embed` and
  `make graphify <target>`. `make test-graphify` is now `make test-py`.
- **No summarizing model inside the stack.** The GGUF model the index job
  could load is removed, with `SUMMARIZE`, `SUMMARY_LIMIT`, `LLM_MODEL_PATH`,
  `LLM_THREADS` and `LLM_CTX`. Model summaries come from the server named by
  `SUMMARIZE_SERVER_URL` or from a remote worker; without either, nodes keep
  their `auto` summaries.
- **Server lamps.** The state of the embedding and summary servers the
  dashboard shows is kept in Valkey, so it survives a restart of the API.

### Upgrading

```bash
make backup
git pull
make pull        # or: make build
make up
```

`make up` rewrites `docker-compose.override.yaml` once, so the two new
services get the same read-only mounts as the API. Then check that both
queues run:

```bash
docker compose ps embed summarize
```

## Migration 0031: no foreign keys, no file text

Releases after 0.23.0 carry migration `0031_tables_without_keys`. The schema up
to 0030 was not safe to keep running, and this is why the migration exists.

### What was wrong with the schema up to 0030

- **Dropping a project breaks the database.** On a live install, dropping the
  old project `claude-context-mcp` left a database that could neither be
  migrated nor restored from its own backup. Treat every database in which a
  project was dropped before 0031 as damaged until the checks below say
  otherwise.
- **A project drop deleted one row and trusted a cascade for the rest.** The
  MCP `drop_project` tool and the dashboard ran `DELETE FROM projects` and left
  nodes, edges, hashes, embeddings, settings and links to `ON DELETE CASCADE`.
  Nothing checked what was left: the dropped project's nodes stayed in the
  database for weeks, invisible to every tool.
- **A database with such rows cannot be restored from its own backup.**
  `pg_restore` adds every foreign key back at the end and validates it, the
  first row of a dropped project fails it, and the whole restore rolls back.
  `make restore` of that `.dump` fails every time.
- **A damaged catalog stops every schema change.** One database was found with
  the row type of `project_settings` missing from `pg_type` while `pg_depend`
  still named it. Reads and writes worked; any `DROP` or `ALTER` of that table
  fails with `cache lookup failed for type`.
- **File text was stored in the database.** `code_embeddings.content_chunk`
  held every indexed file cut into chunks, 11 GB on a large install, and its
  vector index took hours to rebuild.

### What 0031 changes

- No table holds a foreign key or a cascade. Tables are tied by plain value
  columns, and the code deletes and re-keys the rows a project owns itself,
  table by table, in one transaction. A test fails when a table naming a
  project is left out of that list.
- Every table that held a key is copied into a new one and the old one is
  dropped. A row is copied only when what it names still exists - its
  project, both nodes of an edge, the record a link belongs to, the skill a
  switch is for - so the rows a dropped project left behind are not carried
  over. A duplicate key keeps the first row instead of failing the migration.

| Before 0031         | From 0031          |
| ------------------- | ------------------ |
| `graph_nodes`       | `nodes`            |
| `graph_edges`       | `edges`            |
| `code_embeddings`   | `chunks`, no text  |
| `file_hashes`       | `indexed_files`    |
| `summary_cache`     | `cached_summaries` |
| `project_settings`  | `settings`         |
| `project_members`   | `org_members`      |
| `skills`            | `agent_skills`     |
| `skill_enablement`  | `skill_switches`   |
| `project_exports`   | `provided_names`   |
| `project_imports`   | `taken_names`      |
| `project_relations` | `declared_links`   |
| `record_nodes`      | `record_links`     |

- `chunks` starts empty. The embedding queue refills it for every project with
  embedding on, and the chunk half of `search_code` fills as the queue drains;
  see [Embedding](embedding.html).
- A project is dropped through the worker API, which names every table.
- Single-project backups (`.sql.gz`) taken before 0031 name the old tables and
  do not restore afterwards. Whole-database `.dump` files do: restore one, then
  `make up` migrates it.

### Upgrading to 0031

```bash
make backup
docker compose stop worker-api mcp-server web viewer
git pull
make build
make up
```

The migration copies `nodes` and `edges` in full: about four minutes for 1.7
million nodes on the default stack, with room on the database volume for one
more copy of both until it commits.

Check the result:

```bash
make status
docker compose logs --tail 5 migrate
```

The schema is `0031` and the log ends with `successfully migrated database to
version: 31`.

### When 0031 fails with cache lookup failed

```text
ERROR: cache lookup failed for type <oid> (SQLSTATE XX000)
```

The catalog of the database is damaged, and no migration can drop or alter the
table it names. The transaction rolls back and the database stays at 30. The
catalog is rebuilt by restoring the backup into a new, empty database:

```bash
docker compose stop worker-api mcp-server web viewer
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE \"$POSTGRES_DB\"" -c "CREATE DATABASE \"$POSTGRES_DB\""'
make restore FILE=<the .dump taken before the upgrade>
make db version
make up
```

`make db version` shows 30 before `make up` applies 0031.

### When the restore fails on a foreign key

```text
ERROR: insert or update on table "graph_nodes" violates foreign key constraint
DETAIL: Key (project)=(<name>) is not present in table "projects".
```

The backup holds rows of projects that were dropped, and its own foreign keys
refuse them. Restore everything but the foreign keys - 0031 drops them anyway:

```bash
DUMP=~/.local/share/enggraph/backups/<the .dump>
docker compose exec -T postgres pg_restore --list < "$DUMP" \
    | grep -v ' FK CONSTRAINT ' \
    | docker compose exec -T postgres sh -c 'cat > /tmp/restore.list'
docker compose exec -T -e PGOPTIONS='-c max_parallel_maintenance_workers=0' postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --verbose --no-owner --no-privileges --single-transaction --exit-on-error -L /tmp/restore.list' < "$DUMP"
make db version
make up
```

The vector index of `code_embeddings` is rebuilt by this restore and takes
hours on a large install, although 0031 drops that table first thing. Adding
`|INDEX public idx_code_embeddings_` to the `grep -v` pattern skips its indexes
and leaves its rows in place.

### Finding rows of dropped projects

Run against a database still at 30, before the upgrade. It only reads:

```sql
SELECT 'graph_nodes' AS kept_in, project, count(*) FROM graph_nodes
 WHERE project NOT IN (SELECT name FROM projects) GROUP BY project
UNION ALL
SELECT 'graph_edges', project, count(*) FROM graph_edges
 WHERE project NOT IN (SELECT name FROM projects) GROUP BY project
UNION ALL
SELECT 'file_hashes', project, count(*) FROM file_hashes
 WHERE project NOT IN (SELECT name FROM projects) GROUP BY project
UNION ALL
SELECT 'summary_cache', project, count(*) FROM summary_cache
 WHERE project NOT IN (SELECT name FROM projects) GROUP BY project
UNION ALL
SELECT 'code_embeddings', project, count(*) FROM code_embeddings
 WHERE project NOT IN (SELECT name FROM projects) GROUP BY project;
```

No rows means the database is clean. Any row is a project that was dropped
while its rows stayed: its backup needs the foreign-key-free restore above.
0031 does not copy those rows, so they are gone once it has run.
