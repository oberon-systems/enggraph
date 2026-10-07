#!/usr/bin/env bash
# Write the database, or one indexed codebase of it, to a file on the host.
#
# Reached through `make backup`. Two modes, and two formats, because pg_dump
# selects by table and never by row while every table here is scoped by a
# `project` column: the whole database is a pg_dump custom archive, and a
# single project is generated plain SQL - COPY blocks in foreign-key order
# wrapped in one transaction, the shape `pg_dump --format=plain` emits, which
# `psql -f` restores.
#
# The dump is redirected on the host rather than written inside the container,
# so the file belongs to the user running make and not to the postgres uid.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# `docker compose` is two words, and a plain "$COMPOSE" would be looked up as
# one binary of that name.
read -r -a compose <<< "${COMPOSE:-docker compose}"

# The credentials live in .env, which make does not source: reading them here
# is what keeps this working for anyone who changed them from the defaults.
env_value() {
    sed -n "s/^$1=//p" .env 2> /dev/null | tail -1
}

pg_user="$(env_value POSTGRES_USER)"
pg_db="$(env_value POSTGRES_DB)"
pg_user="${pg_user:-user}"
pg_db="${pg_db:-context}"

# Values reach SQL as psql variables rather than as interpolated text, so a
# quote in a path cannot end up as a statement of its own. The statement is fed
# on stdin rather than through -c, which does not interpolate variables at all.
psql_query() {
    "${compose[@]}" exec -T postgres psql \
        -U "$pg_user" -d "$pg_db" -qtAX -F '|' -f - "$@"
}

indexed_projects() {
    local names
    names="$(psql_query <<< \
        "SELECT string_agg(name, ', ' ORDER BY name) FROM projects" || true)"
    echo "Indexed: ${names:-none}." >&2
}

# What one index run brings back and what nothing brings back are different
# losses, so they are never added into one number: it is what tells the reader
# whether the file matters. A project drop splits the same counts differently -
# there, plans survive the drop, and here they only survive in this file.
report_project() {
    local row root indexed nodes edges hashes embeddings plans manual
    local memories suggestions
    row="$(psql_query -v name="$1" <<< "
        SELECT p.root_path,
               coalesce(to_char(p.indexed_at, 'YYYY-MM-DD HH24:MI'), 'never'),
               (SELECT count(*) FROM nodes g WHERE g.project = p.name),
               (SELECT count(*) FROM edges e WHERE e.project = p.name),
               (SELECT count(*) FROM indexed_files f WHERE f.project = p.name),
               (SELECT count(*) FROM chunks c WHERE c.project = p.name),
               (SELECT count(*) FROM nodes g
                  WHERE g.project = '_plans'
                    AND g.metadata ->> 'about' = p.name),
               (SELECT count(*) FROM nodes g WHERE g.type = 'memory'
                  AND g.metadata ->> 'about' = p.name),
               (SELECT count(*) FROM nodes g WHERE g.type = 'suggestion'
                  AND g.metadata ->> 'about' = p.name),
               (SELECT count(*) FROM nodes g WHERE g.project = p.name
                  AND g.metadata ->> 'summary_source' = 'manual')
          FROM projects p
         WHERE p.name = :'name'")"
    IFS='|' read -r root indexed nodes edges hashes embeddings plans \
        memories suggestions manual <<< "$row"
    echo "project \"$1\" ($root, indexed $indexed)"
    echo "  rebuilt by one index run: $nodes nodes, $edges edges," \
        "$hashes file hashes, $embeddings embeddings"
    echo "  not rebuilt, gone for good: $plans plans, $manual manual summaries"
    # Memories and suggestions about this project live under the built-in
    # projects that hold them, and a per-project file is one project. Saying
    # so beats a file that looks complete and is not.
    if [ "$memories" != "0" ] || [ "$suggestions" != "0" ]; then
        echo "  about it but not in this file: $memories memories," \
            "$suggestions suggestions - they live in _memory and" \
            "_suggestions, and travel in the whole-database archive"
    fi
}

if [ -n "${KEEP:-}" ] && ! [[ "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
    echo "KEEP must be a positive number, got \"$KEEP\"." >&2
    exit 1
fi

# Named apart from PROJECT_PATH and PROJECT_NAME, which compose reads out of
# .env for the graphify service: an empty one exported here would reach it as a
# mount of nothing and fail every call in this script.
name="${BACKUP_NAME:-}"
path="${BACKUP_PATH:-}"

if [ -n "$name" ] && [ -n "$path" ]; then
    echo "Pass PROJECT= or PROJECT_NAME=, not both." >&2
    exit 1
fi

if [ -n "$path" ]; then
    # root_path is UNIQUE, so resolving through it is exact - and it avoids
    # reimplementing the name derivation the indexer does in Python.
    if ! name="$(psql_query -v path="$path" <<< \
        "SELECT name FROM projects WHERE root_path = :'path'")"; then
        echo "Cannot reach the database. Is the stack up? Try 'make up'." >&2
        exit 1
    fi
    if [ -z "$name" ]; then
        echo "No project is indexed from $path." >&2
        indexed_projects
        exit 1
    fi
fi

if [ -n "$name" ]; then
    if ! found="$(psql_query -v name="$name" <<< \
        "SELECT name FROM projects WHERE name = :'name'")"; then
        echo "Cannot reach the database. Is the stack up? Try 'make up'." >&2
        exit 1
    fi
    if [ -z "$found" ]; then
        echo "No project named \"$name\"." >&2
        indexed_projects
        exit 1
    fi
fi

backup_dir="${BACKUP_DIR:-}"
backup_dir="${backup_dir:-$HOME/.local/share/enggraph/backups}"
stamp="$(date +%Y%m%d-%H%M%S)"
suffix="$([ -n "$name" ] && echo "sql.gz" || echo "dump")"
dest="${BACKUP_FILE:-$backup_dir/${name:-context}-$stamp.$suffix}"

mkdir -p "$(dirname "$dest")"

# Half a dump must never look like a backup, so the file takes its final name
# only after it has been read back.
part="$dest.part"
trap 'rm -f "$part"' EXIT

if [ -z "$name" ]; then
    "${compose[@]}" exec -T postgres pg_dump -U "$pg_user" -d "$pg_db" \
        --format=custom --no-owner --no-privileges > "$part"
    # Owner and privileges are left out so the archive survives a changed
    # POSTGRES_USER, which is otherwise the one difference that makes a
    # restore fail on a machine that is not the one the dump came from.
    if ! entries="$("${compose[@]}" exec -T postgres pg_restore --list \
        < "$part" | grep -vc '^;')" || [ "$entries" -eq 0 ]; then
        echo "The archive does not read back, not keeping it." >&2
        exit 1
    fi
else
    # ON_ERROR_STOP matters more here than usual: without it psql reports
    # success over a file whose middle COPY produced nothing.
    #
    # \qecho writes the literal lines and COPY writes the data, both to the
    # query output stream, so they interleave in order. A backslash inside a
    # single-quoted \qecho argument is an escape, which is why the COPY
    # terminator is written doubled.
    "${compose[@]}" exec -T postgres psql -U "$pg_user" -d "$pg_db" \
        -qAtX -v ON_ERROR_STOP=1 -v name="$name" -f - << 'SQL' \
        | gzip -9 > "$part"
BEGIN ISOLATION LEVEL REPEATABLE READ;

SELECT root_path AS root,
       coalesce(to_char(indexed_at, 'YYYY-MM-DD HH24:MI'), 'never') AS indexed
  FROM projects WHERE name = :'name' \gset

\qecho '-- enggraph single-project backup'
\qecho '-- project:' :name
\qecho '-- root_path:' :root
\qecho '-- indexed_at:' :indexed
SELECT format('-- created: %s', to_char(now(), 'YYYY-MM-DD HH24:MI:SS'));
\qecho ''
\qecho 'BEGIN;'
-- No table holds a key, so every table naming the project is cleared by name,
-- and the plans about it too, which the COPY below would collide with.
SELECT format($fmt$DELETE FROM skill_switches WHERE skill_id IN
    (SELECT id FROM agent_skills WHERE project = %1$L);
DELETE FROM nodes WHERE project = %1$L;
DELETE FROM edges WHERE project = %1$L;
DELETE FROM chunks WHERE project = %1$L;
DELETE FROM indexed_files WHERE project = %1$L;
DELETE FROM cached_summaries WHERE project = %1$L;
DELETE FROM settings WHERE project = %1$L;
DELETE FROM org_members WHERE organization = %1$L OR project = %1$L;
DELETE FROM agent_skills WHERE project = %1$L;
DELETE FROM skill_switches WHERE project = %1$L;
DELETE FROM provided_names WHERE project = %1$L;
DELETE FROM taken_names WHERE project = %1$L;
DELETE FROM declared_links WHERE source_project = %1$L OR target_project = %1$L;
DELETE FROM record_links WHERE project = %1$L;
DELETE FROM projects WHERE name = %1$L;
DELETE FROM nodes WHERE project = '_plans' AND metadata ->> 'about' = %1$L;$fmt$,
    :'name');

\qecho 'COPY projects (name, root_path, indexed_at, type, description,'
\qecho '               formats, formats_at) FROM stdin;'
COPY (SELECT name, root_path, indexed_at, type, description, formats,
             formats_at
        FROM projects WHERE name = :'name') TO STDOUT;
\qecho '\\.'

-- What the project prunes. The global default under '_settings' belongs to no
-- single project and travels in the whole-database archive instead.
\qecho 'COPY settings (project, ignore_patterns, settings,'
\qecho '                       updated_at) FROM stdin;'
COPY (SELECT project, ignore_patterns, settings, updated_at
        FROM settings WHERE project = :'name') TO STDOUT;
\qecho '\\.'

\qecho 'COPY nodes (project, id, name, type, file_path, content,'
\qecho '                  summary, metadata, created_at) FROM stdin;'
COPY (SELECT project, id, name, type, file_path, content, summary, metadata,
             created_at
        FROM nodes WHERE project = :'name') TO STDOUT;
\qecho '\\.'

\qecho 'COPY edges (project, source_id, target_id, relation_type,'
\qecho '                  metadata) FROM stdin;'
COPY (SELECT project, source_id, target_id, relation_type, metadata
        FROM edges WHERE project = :'name') TO STDOUT;
\qecho '\\.'

\qecho 'COPY chunks (project, node_id, kind, chunk_index, start_line, end_line,'
\qecho '             words, content_hash, model, chunk_chars, chunker, embedding,'
\qecho '             updated_at) FROM stdin;'
COPY (SELECT project, node_id, kind, chunk_index, start_line, end_line, words,
             content_hash, model, chunk_chars, chunker, embedding, updated_at
        FROM chunks WHERE project = :'name') TO STDOUT;
\qecho '\\.'

-- Plans are nodes of the built-in '_plans' project, which the COPY needs to
-- exist before it runs and which a single-project file does not otherwise
-- carry. A global plan is about no project and belongs in no single-project
-- file; it travels in the whole-database archive instead.
\qecho 'INSERT INTO projects (name, root_path, type)'
\qecho "  VALUES ('_plans', 'plans://agent', 'plans')"
\qecho '  ON CONFLICT (name) DO NOTHING;'

\qecho 'COPY nodes (project, id, name, type, file_path, content,'
\qecho '                  summary, metadata, created_at) FROM stdin;'
COPY (SELECT project, id, name, type, file_path, content, summary, metadata,
             created_at
        FROM nodes
       WHERE project = '_plans'
         AND metadata ->> 'about' = :'name') TO STDOUT;
\qecho '\\.'

\qecho 'COPY indexed_files (project, file_path, hash, updated_at) FROM stdin;'
COPY (SELECT project, file_path, hash, updated_at
        FROM indexed_files WHERE project = :'name') TO STDOUT;
\qecho '\\.'

\qecho 'COPY provided_names (project, kind, name, node_id, origin,'
\qecho '                      created_at) FROM stdin;'
COPY (SELECT project, kind, name, node_id, origin, created_at
        FROM provided_names WHERE project = :'name') TO STDOUT;
\qecho '\\.'

\qecho 'COPY taken_names (project, kind, name, source_id, relation_type)'
\qecho '  FROM stdin;'
COPY (SELECT project, kind, name, source_id, relation_type
        FROM taken_names WHERE project = :'name') TO STDOUT;
\qecho '\\.'

-- Either end may be another project the restoring database does not hold, so
-- each relation is written as an insert that skips itself rather than fails.
SELECT format(
    $fmt$INSERT INTO declared_links (source_project, source_id,
           target_project, target_id, relation_type, note, created_at)
         SELECT %L, %L, %L, %L, %L, %L, %L
          WHERE EXISTS (SELECT 1 FROM projects WHERE name = %L)
            AND EXISTS (SELECT 1 FROM projects WHERE name = %L)
         ON CONFLICT DO NOTHING;$fmt$,
    source_project, source_id, target_project, target_id, relation_type,
    note, created_at, source_project, target_project)
  FROM declared_links
 WHERE source_project = :'name' OR target_project = :'name';

-- What memories, plans and suggestions say about this code; a record the
-- restoring database does not hold is skipped rather than failed.
SELECT format(
    $fmt$INSERT INTO record_links (record_project, record_id, project,
           node_id, relation, created_at)
         SELECT %L, %L, %L, %L, %L, %L
          WHERE EXISTS (SELECT 1 FROM nodes
                         WHERE project = %L AND id = %L)
         ON CONFLICT DO NOTHING;$fmt$,
    record_project, record_id, project, node_id, relation, created_at,
    record_project, record_id)
  FROM record_links
 WHERE project = :'name';

\qecho ''
\qecho 'COMMIT;'
COMMIT;
SQL
    if ! gzip -t "$part" 2> /dev/null \
        || [ "$(gzip -cd "$part" | tail -1)" != "COMMIT;" ]; then
        echo "The dump is incomplete, not keeping it." >&2
        exit 1
    fi
fi

mv "$part" "$dest"

if [ -n "$name" ]; then
    report_project "$name"
else
    projects="$(psql_query <<< "SELECT name FROM projects ORDER BY name")"
    if [ -z "$projects" ]; then
        echo "The database holds no project."
    else
        while read -r one; do report_project "$one"; done <<< "$projects"
    fi
fi

echo "wrote $dest ($(du -h "$dest" | cut -f1))"

# Rotation applies to the naming scheme this script owns, so an explicit FILE=
# is never pruned and never counts. Each family rotates alone: keeping two
# whole-database archives must not delete a project's only backup.
if [ -n "${KEEP:-}" ] && [ -z "${BACKUP_FILE:-}" ]; then
    # The stamp sorts lexically, so "newest" is the tail of a plain sort.
    mapfile -t family < <(find "$backup_dir" -maxdepth 1 -type f \
        -name "${name:-context}-*.$suffix" | sort)
    if [ "${#family[@]}" -gt "$KEEP" ]; then
        for old in "${family[@]:0:${#family[@]}-KEEP}"; do
            rm -f "$old"
            echo "pruned $old"
        done
    fi
fi
