-- A project that reads named directories has no tree of its own.
--
-- `projects.root_path` was "the primary source, the first one registered", so
-- whichever slice arrived first stood in for the whole project. That reads
-- wrong for a monorepo cut into slices and plainly wrong for a thematic
-- container - a project whose only job is to hold others so one search reaches
-- all of them. `acme` collecting the trees under ~/acme reported the path
-- of whichever project moved in first as its own root.
--
-- The column now names a tree only when the project is one: a project mounted
-- whole - the unnamed source - keeps its path, and everything else carries the
-- synthetic `registered://<name>` a project registered before it read anything
-- has always had. `ctxgraph.storage.set_primary` writes that rule; this
-- migration applies it to the rows already stored.
--
-- The column stays NOT NULL UNIQUE, and `registered://<name>` is unique
-- because the name is. Nothing addresses a container by a host path: the
-- worker API resolves a directory through `project_sources` instead, and the
-- dashboard lists the directories rather than the column.

-- +goose Up

-- Built-in projects hold records rather than files and carry a
-- `memory://agent`-style root that is not a path either; they have no sources
-- at all, so they are excluded by name rather than by shape.
UPDATE projects
SET root_path = 'registered://' || name
WHERE
    type NOT IN ('memory', 'plans', 'suggestions', 'settings')
    AND EXISTS (
        SELECT 1
        FROM project_sources AS s
        WHERE s.project = projects.name
    )
    AND NOT EXISTS (
        SELECT 1
        FROM project_sources AS s
        WHERE s.project = projects.name AND s.alias = ''
    );

-- +goose Down

-- Back to the first source a project holds, which is what the column meant
-- before. A project reading nothing keeps the synthetic root either way.
UPDATE projects
SET root_path = source.root_path
FROM (
    SELECT DISTINCT ON (project)
        project,
        root_path
    FROM project_sources
    ORDER BY project, created_at, alias
) AS source
WHERE
    projects.name = source.project
    AND projects.root_path <> source.root_path;
