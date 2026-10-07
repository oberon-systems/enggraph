---
name: database
description: Design schemas, write migrations and write code that reads or writes a relational database - plain tables only, no foreign keys or cascades, new features in new tables, ALTER only to change a column's type or size and only with the user's explicit permission. Use before writing or reviewing any migration, DDL, or SQL in application code.
---

# Database

A database here is a set of plain tables and nothing more. Every rule below
exists because relations, cascades and in-place schema changes broke a
production restore: rows a cascade should have taken outlived it, and the
dump could not be loaded back under its own constraints.

## Tables

- **No relations, ever.** No `REFERENCES`, no `FOREIGN KEY`, no
  `ON DELETE` or `ON UPDATE CASCADE`, no triggers that write to other
  tables. Tables are tied by plain value columns (`project`, `node_id`,
  `owner_id`) that the code keeps consistent.
- **Inside one table, anything goes**: primary keys, unique indexes, `CHECK`
  constraints, `NOT NULL`, defaults, any index.
- **A new feature is a new table.** It holds the new columns and the value
  columns that tie it to existing rows. An existing table is never widened
  for a feature.
- **No file contents or other bulk text that lives elsewhere.** Store a
  pointer to it - a path, a line range, a hash - and read it from its source.

## ALTER

- `ALTER TABLE` is allowed for one thing only: changing a column's data type
  or size (`VARCHAR(64)` to `VARCHAR(255)`, `INTEGER` to `BIGINT`, a vector
  dimension).
- Each such `ALTER` needs the user's explicit permission, asked for that
  statement and that migration. Approval of a plan or of an earlier `ALTER`
  is not permission.
- Everything else `ALTER` could do is done another way: a new column or
  constraint means a new table; a renamed or reshaped table is copied into a
  new table under a new name, and the code moves to the new name:
  `CREATE TABLE`, `INSERT ... SELECT`, `DROP TABLE`.

## Cleanup in code

What a cascade would have done, the code does explicitly, in the same
transaction as the change that requires it.

- Keep one list of every table and column that refers to a parent row, next
  to the code that deletes or renames the parent, and walk it there.
- Delete the parent first and the dependent rows after, in separate
  statements, so rows a concurrent writer committed in between are seen.
- A writer that adds a dependent row locks the parent row first
  (`SELECT ... FOR KEY SHARE`) and refuses when it is gone.
- A test reads every table carrying a parent column from
  `information_schema.columns` and fails when one is missing from that list,
  and another asserts that a delete leaves no dependent row behind.
- A test asserts that `information_schema.table_constraints` holds no
  `FOREIGN KEY`.

## Migrations

- One migration per change, numbered, never edited once applied.
- It finishes in minutes on the smallest stack the project supports: no
  `UPDATE` over every row of a large table, no rebuild of a large vector
  index. Load a copied table first and build its indexes after.
- Run it against a copy of real data before it reaches a live database: a
  fresh database proves the SQL, not the time or the data.
- A whole-database backup is taken before every upgrade that applies one.
- A repository that can host a pre-commit hook rejects `REFERENCES`,
  `FOREIGN KEY` and `CASCADE` in new migrations, and any `ALTER TABLE` other
  than a single column type change.

## Live databases

- Run no query against a live database without the user's permission for
  that query.
- Prefer reading the catalog and the statistics views to scanning tables.
