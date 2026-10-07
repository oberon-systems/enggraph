-- Every table that held a key is copied into a new one without it; the code
-- does the cleanup itself. Chunks keep words and vectors, never file text.

-- +goose Up

SET LOCAL lock_timeout = '30s';
SET LOCAL max_parallel_maintenance_workers = 0;
SET LOCAL maintenance_work_mem = '512MB';

CREATE TABLE chunks (
    project VARCHAR(64) NOT NULL,
    node_id VARCHAR(255) NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'source',
    chunk_index INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    words TSVECTOR NOT NULL,
    content_hash VARCHAR(32) NOT NULL,
    model VARCHAR(128) NOT NULL,
    chunk_chars INTEGER NOT NULL DEFAULT 0,
    chunker INTEGER NOT NULL DEFAULT 1,
    embedding VECTOR(768),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, node_id, kind, chunk_index)
);

CREATE INDEX idx_chunks_hash ON chunks (project, content_hash);

CREATE INDEX idx_chunks_words ON chunks USING gin (words);

CREATE INDEX idx_chunks_vector
ON chunks USING hnsw (embedding vector_cosine_ops);

DROP TABLE code_embeddings;

DROP VIEW project_links;

-- Only rows whose project, node, record or skill still exists are copied, and
-- a duplicate key keeps the first row: older databases hold both.
CREATE TABLE nodes (
    project VARCHAR(64) NOT NULL,
    id VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(50) NOT NULL,
    file_path TEXT,
    content TEXT,
    summary TEXT,
    metadata JSONB DEFAULT '{}'::JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_nodes_key ON nodes (project, id);

INSERT INTO nodes (
    project, id, name, type, file_path, content, summary, metadata, created_at
)
SELECT
    g.project,
    g.id,
    g.name,
    g.type,
    g.file_path,
    g.content,
    g.summary,
    g.metadata,
    g.created_at
FROM graph_nodes AS g
WHERE
    EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = g.project
    )
ON CONFLICT DO NOTHING;

CREATE INDEX idx_nodes_type ON nodes (project, type);

CREATE INDEX idx_nodes_file_path ON nodes (project, file_path);

CREATE INDEX idx_nodes_name_trgm ON nodes USING gin (name gin_trgm_ops);

CREATE INDEX idx_nodes_id_trgm ON nodes USING gin (id gin_trgm_ops);

CREATE INDEX idx_nodes_plan ON nodes ((metadata ->> 'status'))
WHERE type IN ('plan', 'template');

CREATE INDEX idx_nodes_suggestion ON nodes ((metadata ->> 'status'))
WHERE type = 'suggestion';

CREATE TABLE edges (
    project VARCHAR(64) NOT NULL,
    source_id VARCHAR(255) NOT NULL,
    target_id VARCHAR(255) NOT NULL,
    relation_type VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}'::JSONB
);

CREATE UNIQUE INDEX idx_edges_key ON edges (
    project, source_id, target_id, relation_type
);

INSERT INTO edges (project, source_id, target_id, relation_type, metadata)
SELECT
    e.project,
    e.source_id,
    e.target_id,
    e.relation_type,
    e.metadata
FROM graph_edges AS e
WHERE
    EXISTS (
        SELECT 1 FROM nodes AS s
        WHERE s.project = e.project AND s.id = e.source_id
    )
    AND EXISTS (
        SELECT 1 FROM nodes AS t
        WHERE t.project = e.project AND t.id = e.target_id
    )
ON CONFLICT DO NOTHING;

CREATE INDEX idx_edges_target ON edges (project, target_id);

CREATE TABLE indexed_files (
    project VARCHAR(64) NOT NULL,
    file_path TEXT NOT NULL,
    hash VARCHAR(32) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, file_path)
);

INSERT INTO indexed_files (project, file_path, hash, updated_at)
SELECT
    f.project,
    f.file_path,
    f.hash,
    f.updated_at
FROM file_hashes AS f
WHERE
    EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = f.project
    )
ON CONFLICT DO NOTHING;

CREATE TABLE cached_summaries (
    project VARCHAR(64) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    summary TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, content_hash)
);

INSERT INTO cached_summaries (project, content_hash, summary, updated_at)
SELECT
    c.project,
    c.content_hash,
    c.summary,
    c.updated_at
FROM summary_cache AS c
WHERE
    EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = c.project
    )
ON CONFLICT DO NOTHING;

CREATE TABLE settings (
    project VARCHAR(64) PRIMARY KEY,
    ignore_patterns TEXT,
    settings JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- +goose StatementBegin
INSERT INTO settings (project, ignore_patterns, settings, updated_at)
SELECT
    s.project,
    REPLACE(
        s.ignore_patterns,
        $old$# Secrets. Pruned by the indexer even when removed from here: the text
# of every indexed file is stored in the graph.$old$,
        '# Secrets. Pruned by the indexer even when removed from here.'
    ) AS ignore_patterns,
    s.settings,
    s.updated_at
FROM project_settings AS s
WHERE
    EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = s.project
    )
ON CONFLICT DO NOTHING;
-- +goose StatementEnd

CREATE TABLE org_members (
    organization VARCHAR(64) NOT NULL,
    project VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    owned BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (organization, project),
    CONSTRAINT org_members_not_itself CHECK (organization <> project)
);

CREATE UNIQUE INDEX idx_org_members_one_owner ON org_members (project)
WHERE owned;

INSERT INTO org_members (organization, project, created_at, owned)
SELECT
    m.organization,
    m.project,
    m.created_at,
    m.owned
FROM project_members AS m
WHERE
    m.organization <> m.project
    AND EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = m.organization
    )
    AND EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = m.project
    )
ON CONFLICT DO NOTHING;

CREATE INDEX idx_org_members_project ON org_members (project);

CREATE TABLE agent_skills (
    id SERIAL PRIMARY KEY,
    project VARCHAR(64),
    name VARCHAR(64) NOT NULL,
    content TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    source VARCHAR(16) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_skills_source CHECK (source IN ('repo', 'import')),
    CONSTRAINT agent_skills_repo_global CHECK (
        source = 'import' OR project IS NULL
    )
);

CREATE UNIQUE INDEX idx_agent_skills_scope_name ON agent_skills (
    COALESCE(project, ''), name
);

INSERT INTO agent_skills (
    id, project, name, content, sha256, source, updated_at
)
SELECT
    k.id,
    k.project,
    k.name,
    k.content,
    k.sha256,
    k.source,
    k.updated_at
FROM skills AS k
WHERE
    k.project IS NULL
    OR EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = k.project
    )
ON CONFLICT DO NOTHING;

SELECT
    SETVAL(
        PG_GET_SERIAL_SEQUENCE('agent_skills', 'id'),
        COALESCE(MAX(id), 0) + 1,
        FALSE
    )
FROM agent_skills;

CREATE TABLE skill_switches (
    project VARCHAR(64) NOT NULL,
    skill_id INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL,
    PRIMARY KEY (project, skill_id)
);

INSERT INTO skill_switches (project, skill_id, enabled)
SELECT
    w.project,
    w.skill_id,
    w.enabled
FROM skill_enablement AS w
WHERE
    EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = w.project
    )
    AND EXISTS (
        SELECT 1 FROM agent_skills AS k
        WHERE k.id = w.skill_id
    )
ON CONFLICT DO NOTHING;

CREATE TABLE provided_names (
    project VARCHAR(64) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    name VARCHAR(255) NOT NULL,
    node_id VARCHAR(255) NOT NULL DEFAULT './',
    origin VARCHAR(16) NOT NULL DEFAULT 'auto',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, kind, name),
    CONSTRAINT provided_names_origin CHECK (origin IN ('auto', 'manual'))
);

INSERT INTO provided_names (project, kind, name, node_id, origin, created_at)
SELECT
    x.project,
    x.kind,
    x.name,
    x.node_id,
    x.origin,
    x.created_at
FROM project_exports AS x
WHERE
    EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = x.project
    )
ON CONFLICT DO NOTHING;

CREATE INDEX idx_provided_names_name ON provided_names (kind, name);

CREATE TABLE taken_names (
    project VARCHAR(64) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    name VARCHAR(255) NOT NULL,
    source_id VARCHAR(255) NOT NULL,
    relation_type VARCHAR(50) NOT NULL,
    PRIMARY KEY (project, kind, name, source_id, relation_type)
);

INSERT INTO taken_names (project, kind, name, source_id, relation_type)
SELECT
    i.project,
    i.kind,
    i.name,
    i.source_id,
    i.relation_type
FROM project_imports AS i
WHERE
    EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = i.project
    )
ON CONFLICT DO NOTHING;

CREATE INDEX idx_taken_names_name ON taken_names (kind, name);

CREATE TABLE declared_links (
    id SERIAL PRIMARY KEY,
    source_project VARCHAR(64) NOT NULL,
    source_id VARCHAR(255) NOT NULL DEFAULT './',
    target_project VARCHAR(64) NOT NULL,
    target_id VARCHAR(255) NOT NULL DEFAULT './',
    relation_type VARCHAR(50) NOT NULL,
    note TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT declared_links_once UNIQUE (
        source_project, source_id, target_project, target_id, relation_type
    ),
    CONSTRAINT declared_links_not_itself CHECK (
        source_project <> target_project
    )
);

INSERT INTO declared_links (
    source_project, source_id, target_project, target_id, relation_type,
    note, created_at
)
SELECT
    r.source_project,
    r.source_id,
    r.target_project,
    r.target_id,
    r.relation_type,
    r.note,
    r.created_at
FROM project_relations AS r
WHERE
    r.source_project <> r.target_project
    AND EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = r.source_project
    )
    AND EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = r.target_project
    )
ON CONFLICT DO NOTHING;

CREATE INDEX idx_declared_links_target ON declared_links (target_project);

CREATE TABLE record_links (
    record_project VARCHAR(64) NOT NULL,
    record_id VARCHAR(255) NOT NULL,
    project VARCHAR(64) NOT NULL,
    node_id VARCHAR(255) NOT NULL,
    relation VARCHAR(50) NOT NULL DEFAULT 'about',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (record_project, record_id, project, node_id)
);

-- A link to code an index run dropped is kept: the record shows it missing.
INSERT INTO record_links (
    record_project, record_id, project, node_id, relation, created_at
)
SELECT
    l.record_project,
    l.record_id,
    l.project,
    l.node_id,
    l.relation,
    l.created_at
FROM record_nodes AS l
WHERE
    EXISTS (
        SELECT 1 FROM projects AS p
        WHERE p.name = l.project
    )
    AND EXISTS (
        SELECT 1 FROM nodes AS n
        WHERE n.project = l.record_project AND n.id = l.record_id
    )
ON CONFLICT DO NOTHING;

CREATE INDEX idx_record_links_node ON record_links (project, node_id);

DROP TABLE record_nodes;
DROP TABLE graph_edges;
DROP TABLE skill_enablement;
DROP TABLE skills;
DROP TABLE project_relations;
DROP TABLE project_imports;
DROP TABLE project_exports;
DROP TABLE project_members;
DROP TABLE project_settings;
DROP TABLE summary_cache;
DROP TABLE file_hashes;
DROP TABLE graph_nodes;

-- A name is linked only when one other project provides it, and never when
-- the project taking it provides it too.
CREATE VIEW project_links AS
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
FROM taken_names AS i
INNER JOIN provided_names AS e
    ON i.kind = e.kind AND i.name = e.name AND i.project <> e.project
WHERE
    NOT EXISTS (
        SELECT 1 FROM provided_names AS x
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
FROM declared_links AS r;

-- +goose Down

-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'not reversible: restore the backup taken before 0031';
END
$$;
-- +goose StatementEnd
