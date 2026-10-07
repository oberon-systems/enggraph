"""The whole schema, as goose left it after its migration 0031.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EXTENSIONS = (
    "CREATE EXTENSION IF NOT EXISTS vector;",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
)

# Splits camelCase before the simple parser sees it, for lexical search.
FUNCTIONS = (
    r"""
    CREATE OR REPLACE FUNCTION lexical_words(body TEXT) RETURNS TSVECTOR
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
        SELECT to_tsvector(
            'simple'::REGCONFIG,
            regexp_replace(
                regexp_replace(body, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                '([A-Z]+)([A-Z][a-z])', '\1 \2', 'g'
            )
        )
    $$;
    """,
)

TABLES = (
    """
    CREATE TABLE projects (
        name VARCHAR(64) PRIMARY KEY,
        root_path TEXT NOT NULL UNIQUE,
        indexed_at TIMESTAMP WITH TIME ZONE,
        type VARCHAR(50) NOT NULL DEFAULT 'codebase',
        description TEXT,
        formats TEXT [] NOT NULL DEFAULT '{}',
        formats_at TIMESTAMP WITH TIME ZONE
    );
    """,
    """
    CREATE TABLE index_jobs (
        id SERIAL PRIMARY KEY,
        project VARCHAR(64) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'running',
        fresh BOOLEAN NOT NULL DEFAULT FALSE,
        project_type VARCHAR(50),
        files INTEGER,
        with_node INTEGER,
        entities INTEGER,
        edges INTEGER,
        pruned INTEGER,
        failures INTEGER,
        gaps INTEGER,
        error TEXT,
        started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP WITH TIME ZONE
    );
    """,
    """
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
    """,
    """
    CREATE TABLE edges (
        project VARCHAR(64) NOT NULL,
        source_id VARCHAR(255) NOT NULL,
        target_id VARCHAR(255) NOT NULL,
        relation_type VARCHAR(50) NOT NULL,
        metadata JSONB DEFAULT '{}'::JSONB
    );
    """,
    """
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
    """,
    """
    CREATE TABLE indexed_files (
        project VARCHAR(64) NOT NULL,
        file_path TEXT NOT NULL,
        hash VARCHAR(32) NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (project, file_path)
    );
    """,
    """
    CREATE TABLE cached_summaries (
        project VARCHAR(64) NOT NULL,
        content_hash VARCHAR(64) NOT NULL,
        summary TEXT NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (project, content_hash)
    );
    """,
    """
    CREATE TABLE settings (
        project VARCHAR(64) PRIMARY KEY,
        ignore_patterns TEXT,
        settings JSONB NOT NULL DEFAULT '{}',
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE org_members (
        organization VARCHAR(64) NOT NULL,
        project VARCHAR(64) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        owned BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (organization, project),
        CONSTRAINT org_members_not_itself CHECK (organization <> project)
    );
    """,
    """
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
    """,
    """
    CREATE TABLE skill_switches (
        project VARCHAR(64) NOT NULL,
        skill_id INTEGER NOT NULL,
        enabled BOOLEAN NOT NULL,
        PRIMARY KEY (project, skill_id)
    );
    """,
    """
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
    """,
    """
    CREATE TABLE taken_names (
        project VARCHAR(64) NOT NULL,
        kind VARCHAR(16) NOT NULL,
        name VARCHAR(255) NOT NULL,
        source_id VARCHAR(255) NOT NULL,
        relation_type VARCHAR(50) NOT NULL,
        PRIMARY KEY (project, kind, name, source_id, relation_type)
    );
    """,
    """
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
    """,
    """
    CREATE TABLE record_links (
        record_project VARCHAR(64) NOT NULL,
        record_id VARCHAR(255) NOT NULL,
        project VARCHAR(64) NOT NULL,
        node_id VARCHAR(255) NOT NULL,
        relation VARCHAR(50) NOT NULL DEFAULT 'about',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (record_project, record_id, project, node_id)
    );
    """,
)

INDEXES = (
    "CREATE INDEX idx_projects_type ON projects (type);",
    """
    CREATE UNIQUE INDEX idx_index_jobs_one_running
    ON index_jobs (project) WHERE status = 'running';
    """,
    "CREATE INDEX idx_index_jobs_project ON index_jobs (project, started_at DESC);",
    "CREATE UNIQUE INDEX idx_nodes_key ON nodes (project, id);",
    "CREATE INDEX idx_nodes_type ON nodes (project, type);",
    "CREATE INDEX idx_nodes_file_path ON nodes (project, file_path);",
    "CREATE INDEX idx_nodes_name_trgm ON nodes USING gin (name gin_trgm_ops);",
    "CREATE INDEX idx_nodes_id_trgm ON nodes USING gin (id gin_trgm_ops);",
    """
    CREATE INDEX idx_nodes_plan ON nodes ((metadata ->> 'status'))
    WHERE type IN ('plan', 'template');
    """,
    """
    CREATE INDEX idx_nodes_suggestion ON nodes ((metadata ->> 'status'))
    WHERE type = 'suggestion';
    """,
    """
    CREATE UNIQUE INDEX idx_edges_key ON edges (
        project, source_id, target_id, relation_type
    );
    """,
    "CREATE INDEX idx_edges_target ON edges (project, target_id);",
    "CREATE INDEX idx_chunks_hash ON chunks (project, content_hash);",
    "CREATE INDEX idx_chunks_words ON chunks USING gin (words);",
    """
    CREATE INDEX idx_chunks_vector
    ON chunks USING hnsw (embedding vector_cosine_ops);
    """,
    """
    CREATE UNIQUE INDEX idx_org_members_one_owner ON org_members (project)
    WHERE owned;
    """,
    "CREATE INDEX idx_org_members_project ON org_members (project);",
    """
    CREATE UNIQUE INDEX idx_agent_skills_scope_name ON agent_skills (
        COALESCE(project, ''), name
    );
    """,
    "CREATE INDEX idx_provided_names_name ON provided_names (kind, name);",
    "CREATE INDEX idx_taken_names_name ON taken_names (kind, name);",
    "CREATE INDEX idx_declared_links_target ON declared_links (target_project);",
    "CREATE INDEX idx_record_links_node ON record_links (project, node_id);",
)

# A name is linked only when one other project provides it, and never when
# the project taking it provides it too.
VIEWS = (
    """
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
        NULL::VARCHAR AS name,
        'declared' AS origin,
        r.note
    FROM declared_links AS r;
    """,
)

# The global ignore default, as the settings page shows it on a new stack.
DEFAULT_IGNORE = """# Secrets. Pruned by the indexer even when removed from here.
.env
.env.*
*.env
!.env.example
!.env.sample
!.env.template
*.pem
*.key
*.crt
*.cer
*.der
*.p12
*.pfx
*.jks
*.keystore
*.ppk
*.gpg
*.kdbx
*.jwt
*.tfvars
*.tfstate
*.tfstate.*
*.sops.*
vault.yml
vault.yaml
*.vault.yml
id_rsa*
id_ecdsa*
id_ed25519*
id_dsa*
authorized_keys
known_hosts
.htpasswd
.netrc
.pgpass
credentials
credentials.json
kubeconfig
.ssh/
.gnupg/
.aws/
.kube/
.docker/config.json

# Agent state rather than code of the tree.
.claude/
.gemini/
.codex/
.cursor/

# Lock files and generated code, saying nothing their source does not.
package-lock.json
npm-shrinkwrap.json
yarn.lock
pnpm-lock.yaml
composer.lock
poetry.lock
Pipfile.lock
Gemfile.lock
Cargo.lock
go.sum
*.min.js
*.min.css
*.map
*_pb2.py
*.pb.go

# Build and tool output the built-in skip list does not cover.
bower_components/
.nuxt/
.svelte-kit/
.turbo/
.parcel-cache/
.angular/
.yarn/
.pnpm-store/
.eggs/
*.egg-info/
htmlcov/
.nyc_output/
.serverless/
.direnv/
.hypothesis/
*.log
"""

# The built-in projects hold records rather than files.
ROWS = (
    """
    INSERT INTO projects (name, root_path, type) VALUES
    ('_memory', 'memory://agent', 'memory'),
    ('_suggestions', 'suggestions://agent', 'suggestions'),
    ('_plans', 'plans://agent', 'plans'),
    ('_settings', 'settings://agent', 'settings');
    """,
    "INSERT INTO settings (project, ignore_patterns) VALUES ('_settings', $doc$"
    + DEFAULT_IGNORE
    + "$doc$);",
)


def upgrade() -> None:
    """Create everything a new database needs."""
    for statement in (*EXTENSIONS, *FUNCTIONS, *TABLES, *INDEXES, *VIEWS, *ROWS):
        op.execute(statement)


def downgrade() -> None:
    """Refuse: below this revision there is nothing to go back to."""
    raise RuntimeError("0001 is the first revision: drop the database instead")
