---
layout: default
title: System Nuances
nav_order: 7
---

## Database schema

The schema is owned by [Alembic](https://alembic.sqlalchemy.org/). The
revisions live in `packages/core/src/enggraph/core/migrations/versions/` and
are baked into the `api` image; the `alembic_version` table records the one
the database is at. The `migrate` service runs that image to completion
before anything else reaches the database, so `make up` applies whatever is
pending on its own.

```bash
make db version        # the revision applied, and the history
make db migrate         # apply now
make db validate        # check the revisions form one chain
make db new NAME=<slug> # write the next numbered revision file
```

A new revision reaches the database only after `make build`: the service
runs the copy inside the image, not the working tree.

Revision `0001` is the whole schema. A revision is DDL handed to
`op.execute`; `packages/core/src/enggraph/core/models.py` names the same
tables as SQLModel classes for code to read rows with, and creates nothing.
A test fails when the two disagree on a table, a column or whether it may be
null.

A database release 0.24.0 migrated is marked as `0001` on the first run and
is not changed. One that stopped earlier is refused, and so is an older
backup restored over a newer database: bring it to 0.24.0, the last release
that carries goose, first. The `schema_migrations` table goose kept is
dropped on that run. The steps from 0.x to 1.0.0 are in
[BREAKING CHANGES](breaking-changes.html#upgrading-from-0x-to-100).

Two things a revision cannot do for you: `scripts/backup.sh` and
`scripts/restore.sh` name every column of every table explicitly, so a
revision that changes one has to update them in the same commit; and a
whole-database backup restores `alembic_version` with the tables, so the
next `make up` applies only what that backup had not seen.

The work queues are not in the database. Embedding tasks, summary jobs with
their tasks, and the lock of a running index live in Valkey, in memory only,
with LRU eviction and nothing written to disk. Any key may be lost; the sweeps
rebuild the queues from the graph, and every result - a vector, a summary, a
skip mark - is written to Postgres.

Core tables:

- `nodes` - one row per file, per code entity (`file_path::name`) and
  per unresolved external import or symbol
- `edges` - typed relations between nodes, unique per
  `(source, target, relation)`
- `chunks` - `vector(768)` chunks with an HNSW cosine index and a
  GIN index over their words, written by the embedding queue rather than by an
  index run. A chunk holds no text: its words are a `tsvector`, and the text
  is read from the mounted tree through the worker API when it is shown. Each row carries the line range it was cut from and the hash of
  the file it was cut from, which is what makes a file re-embedded only when
  it changes
- `settings` - one row per level: the ignore document as a column
  (`ignore_patterns`, summed across the levels), and everything else as one `settings` JSONB. The indexing schedule
  is the key `indexing`, holding `mode`, `interval_minutes` and
  `debounce_minutes`, any of which may be absent - that is the level
  inheriting it from the one above. The switches are `enabled` and
  `server_url`, under `indexing`, `summarize` and `embedding`, resolved the
  same way with one exception: `enabled` false at the global level is a gate,
  and no lower level is asked. An absent `enabled` is not that: it is the
  level declining to answer, which is why nothing writes the key until a
  switch is used - a stored global `false` would take away a project's ability
  to turn itself on. A knob added later is another key rather than another
  migration
- `index_jobs` - one row per index run, the history the dashboard shows. The
  row is opened at start rather than queued, so a run interrupted by a restart
  is closed as failed when the worker API comes back up. What stops a second
  run of a project is a lock in Valkey, not the table
  Plans, memories and suggestions have no table of their own: they are
  `nodes` rows under the built-in projects `_plans`, `_memory` and
  `_suggestions`, created by a migration rather than by an index run. That is
  why they are searchable like any other node, and why a suggestion's status
  and hit count - or a plan's status and the project it is about - live in the
  node's `metadata` rather than in columns.

A plan id is stored as the node id unchanged. It names a topic and is written
by hand, so it is already unique across the database and needs none of the
`<about>/<id>` scoping a memory id gets.

No table holds a foreign key. Dropping a project goes through the worker API,
which deletes from every table naming it in one transaction - nodes, edges,
chunks, hashes, settings, memberships, skills, links - and nothing of any
other project; a test fails when a table is missing from that list. Plans are
not derived and name the project in metadata alone, so they stay. So do
memories and suggestions about that project: they sit under a built-in
project the drop does not touch, and go on naming a codebase that is gone -
the drop report counts them for exactly that reason. The
data directory is a bind mount, not a volume - `docker compose down -v`
does not remove it, and a regenerated `POSTGRES_PASSWORD` needs
`make clean` to take effect, since the entrypoint skips initialisation
while the directory already holds a database.

## Read-Only Access

The system mounts all indexed codebases as read-only (`:ro`). Containers **never** mutate your source files.

## Pure ASCII Policy

To ensure compatibility across all environments and tools, all documentation, comments, commit messages, and source code must use only ASCII characters. Unicode symbols and emojis are prohibited.

## Database Integrity

- **One Database:** A single database holds the graph of every codebase you index.
- **Corruption Risk:** Running multiple database containers over the same data directory will cause irreversible corruption. Do not attempt to run parallel stacks from this repository.
- **Persistence:** `docker compose down` does not remove the database volume. To completely reset the index, use `make clean`.

## MCP and Security

- **DNS Rebinding:** The server implements basic DNS rebinding protection via `ALLOWED_HOSTS` and `ALLOWED_ORIGINS` environment variables.
- **Trust:** When registering the MCP server for Gemini, `trust: true` is recommended only for codebases you own.
- **Permissions:** The Claude counterpart of that flag is the `mcp__enggraph` allow rule `make install` writes to `~/.claude/settings.json`. It covers every tool of the server except the four `drop_*` ones, which stay behind a prompt; `PERMISSIONS=0` skips it entirely.
