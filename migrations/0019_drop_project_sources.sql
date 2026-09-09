-- A project is one tree again, and an organization is how projects group.
--
-- Migration 0011 made a project a selection of host directories, each mounted
-- at /code/<project>/<alias> and stamping its alias onto every node id it
-- produced. Migration 0014 added organizations, which answer the same need
-- without touching a mount: a member keeps its name, its tree, its address and
-- its graph, and belonging somewhere is a row rather than a bind.
--
-- Two mechanisms for one need is one too many, so the directories go. What
-- they read is not thrown away: a directory becomes a project reading that
-- path, and the project that held it becomes the organization those projects
-- were moved into - which is the same arrangement, expressed the one way that
-- is left.
--
-- Three shapes go in, in order of how much moves:
--
--   * one unnamed directory - the classic project, mounted whole. Nothing
--     changes but where the path is stored.
--   * one named directory - the project takes its path and keeps its name.
--     Node ids lose the alias segment, which one plain index run settles:
--     the files appear under their new paths and the old nodes are pruned as
--     files that left the tree.
--   * several named directories - each becomes a project of its own, moved
--     into the holder, which becomes an organization. The holder is no tree
--     any more, so the graph it derived from those directories is deleted
--     here rather than left to point at nothing, and each new project needs
--     one index run.
--
-- A directory whose alias is already the name of a project is named
-- `<holder>-<alias>` instead, and a counter is added if that is taken too.
-- Every name chosen and every graph deleted is raised as a notice.

-- +goose Up

-- Where the last run read each half of the selection from. It lived on the
-- source row, because a source was what a run walked.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS keep_source VARCHAR(16);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS ignore_source VARCHAR(16);

-- +goose StatementBegin
DO $$
DECLARE
    holder RECORD;
    source RECORD;
    candidate TEXT;
    attempt INTEGER;
BEGIN
    FOR holder IN
        SELECT
            p.name,
            p.type,
            count(*) AS sources
        FROM projects AS p
        INNER JOIN project_sources AS s ON s.project = p.name
        GROUP BY p.name, p.type
        HAVING count(*) > 1
        ORDER BY p.name
    LOOP
        FOR source IN
            SELECT alias, root_path, keep_source, ignore_source
            FROM project_sources
            WHERE project = holder.name
            ORDER BY created_at, alias
        LOOP
            -- An alias only had to be unique inside its project, and the
            -- built-in prefix was free to use because it addressed nothing.
            -- A project name is neither.
            candidate := source.alias;
            IF starts_with(candidate, '_') OR EXISTS (
                SELECT 1 FROM projects WHERE name = candidate
            ) THEN
                candidate := left(holder.name || '-' || source.alias, 64);
            END IF;
            attempt := 1;
            WHILE EXISTS (SELECT 1 FROM projects WHERE name = candidate) LOOP
                attempt := attempt + 1;
                candidate := left(
                    holder.name || '-' || source.alias, 60
                ) || '-' || attempt::text;
            END LOOP;

            INSERT INTO projects (
                name, root_path, indexed_at, type, keep_source, ignore_source
            )
            VALUES (
                candidate,
                source.root_path,
                NULL,
                CASE
                    WHEN holder.type = 'organization' THEN 'codebase'
                    ELSE holder.type
                END,
                source.keep_source,
                source.ignore_source
            );
            -- What the directory settled about itself becomes what the
            -- project settles; what the holder settled becomes what the
            -- organization sets, which its members inherit.
            UPDATE project_settings
            SET project = candidate, alias = ''
            WHERE project = holder.name AND alias = source.alias;
            INSERT INTO project_members (organization, project, owned)
            VALUES (holder.name, candidate, TRUE)
            ON CONFLICT DO NOTHING;

            RAISE NOTICE
                'directory % of % is now project %, moved into %',
                source.alias, holder.name, candidate, holder.name;
        END LOOP;

        -- The holder reads nothing now, so it is never indexed again and
        -- nothing would ever prune what it derived from those directories.
        DELETE FROM file_hashes WHERE project = holder.name;
        DELETE FROM graph_nodes WHERE project = holder.name;
        UPDATE projects
        SET
            type = 'organization',
            root_path = 'registered://' || holder.name,
            indexed_at = NULL,
            keep_source = NULL,
            ignore_source = NULL
        WHERE name = holder.name;

        RAISE NOTICE
            'project % is an organization of % projects; index each of them',
            holder.name, holder.sources;
    END LOOP;
END;
$$;
-- +goose StatementEnd

-- Whatever is left holds one directory, which is the tree the project is.
CREATE TEMPORARY TABLE only_source AS
SELECT DISTINCT ON (project)
    project,
    root_path,
    keep_source,
    ignore_source
FROM project_sources
ORDER BY project, created_at, alias;

UPDATE projects
SET
    keep_source = only_source.keep_source,
    ignore_source = only_source.ignore_source,
    root_path = only_source.root_path
FROM only_source
WHERE projects.name = only_source.project;

DROP TABLE only_source;

-- One row per project, so the level a setting was written at is the project
-- itself and no longer one of its directories.
ALTER TABLE project_settings DROP COLUMN IF EXISTS alias;

-- Postgres dropped the primary key with the column it covered.
ALTER TABLE project_settings ADD PRIMARY KEY (project);

ALTER TABLE index_jobs DROP COLUMN IF EXISTS aliases;

DROP TABLE IF EXISTS project_sources;

-- +goose Down

CREATE TABLE IF NOT EXISTS project_sources (
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON UPDATE CASCADE ON DELETE CASCADE,
    alias VARCHAR(64) NOT NULL,
    root_path TEXT NOT NULL UNIQUE,
    keep_source VARCHAR(16),
    ignore_source VARCHAR(16),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, alias)
);

-- One unnamed source per project that is a tree, which is every project the
-- up migration left with a path. The organizations it built are projects of
-- their own now and stay that way: what a rollback restores is the table, not
-- the arrangement it used to hold.
INSERT INTO project_sources (
    project, alias, root_path, keep_source, ignore_source
)
SELECT
    name,
    '' AS source_alias,
    root_path,
    keep_source,
    ignore_source
FROM projects
WHERE
    type NOT IN ('memory', 'plans', 'suggestions', 'settings', 'organization')
ON CONFLICT DO NOTHING;

ALTER TABLE projects DROP COLUMN IF EXISTS ignore_source;

ALTER TABLE projects DROP COLUMN IF EXISTS keep_source;

ALTER TABLE index_jobs ADD COLUMN IF NOT EXISTS aliases TEXT [];

ALTER TABLE project_settings DROP CONSTRAINT IF EXISTS project_settings_pkey;

ALTER TABLE project_settings
ADD COLUMN IF NOT EXISTS alias VARCHAR(64) NOT NULL DEFAULT '';

ALTER TABLE project_settings ADD PRIMARY KEY (project, alias);
