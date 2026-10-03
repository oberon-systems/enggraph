-- Links between projects: what each one provides and takes, joined by a view.
-- Keys reach `projects` only: an index run rewrites nodes and must not cascade.

-- +goose Up

CREATE TABLE IF NOT EXISTS project_exports (
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    kind VARCHAR(16) NOT NULL,
    name VARCHAR(255) NOT NULL,
    node_id VARCHAR(255) NOT NULL DEFAULT './',
    origin VARCHAR(16) NOT NULL DEFAULT 'auto',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, kind, name),
    CONSTRAINT project_exports_origin CHECK (origin IN ('auto', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_project_exports_name ON project_exports (
    kind, name
);

CREATE TABLE IF NOT EXISTS project_imports (
    project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    kind VARCHAR(16) NOT NULL,
    name VARCHAR(255) NOT NULL,
    source_id VARCHAR(255) NOT NULL,
    relation_type VARCHAR(50) NOT NULL,
    PRIMARY KEY (project, kind, name, source_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_project_imports_name ON project_imports (
    kind, name
);

CREATE TABLE IF NOT EXISTS project_relations (
    id SERIAL PRIMARY KEY,
    source_project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    source_id VARCHAR(255) NOT NULL DEFAULT './',
    target_project VARCHAR(64) NOT NULL REFERENCES projects (
        name
    ) ON DELETE CASCADE ON UPDATE CASCADE,
    target_id VARCHAR(255) NOT NULL DEFAULT './',
    relation_type VARCHAR(50) NOT NULL,
    note TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT project_relations_once UNIQUE (
        source_project, source_id, target_project, target_id, relation_type
    ),
    CONSTRAINT project_relations_not_itself CHECK (
        source_project <> target_project
    )
);

CREATE INDEX IF NOT EXISTS idx_project_relations_target ON project_relations (
    target_project
);

-- A name is linked only when one other project provides it, and never when
-- the project taking it provides it too.
CREATE OR REPLACE VIEW project_links AS
SELECT
    i.project AS source_project,
    i.source_id,
    e.project AS target_project,
    e.node_id AS target_id,
    i.relation_type,
    i.kind,
    i.name,
    'matched' AS origin,
    NULL::TEXT AS note
FROM project_imports AS i
INNER JOIN project_exports AS e
    ON i.kind = e.kind AND i.name = e.name AND i.project <> e.project
WHERE
    NOT EXISTS (
        SELECT 1 FROM project_exports AS x
        WHERE x.kind = i.kind AND x.name = i.name AND x.project <> e.project
    )
UNION ALL
SELECT
    r.source_project,
    r.source_id,
    r.target_project,
    r.target_id,
    r.relation_type,
    NULL::VARCHAR AS kind,
    NULL::VARCHAR AS name,  -- noqa: RF04
    'declared' AS origin,
    r.note
FROM project_relations AS r;

-- +goose Down

DROP VIEW IF EXISTS project_links;
DROP INDEX IF EXISTS idx_project_relations_target;
DROP TABLE IF EXISTS project_relations;
DROP INDEX IF EXISTS idx_project_imports_name;
DROP TABLE IF EXISTS project_imports;
DROP INDEX IF EXISTS idx_project_exports_name;
DROP TABLE IF EXISTS project_exports;
