"""Host agents: their enrollment, their credentials, their exports and bindings.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# The stack stores no agent key: it is derived from the master key and the
# credential, so a secret row holds only which generations are valid.
TABLES = (
    """
    CREATE TABLE IF NOT EXISTS agents (
        id VARCHAR(36) NOT NULL,
        name TEXT NOT NULL,
        labels TEXT[] NOT NULL DEFAULT '{}',
        state VARCHAR(16) NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'enabled', 'disabled')),
        version TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        approved_at TIMESTAMP WITH TIME ZONE,
        approved_by TEXT,
        last_seen_at TIMESTAMP WITH TIME ZONE,
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_secrets (
        agent_id VARCHAR(36) NOT NULL,
        kid VARCHAR(64) NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0,
        pending_generation INTEGER,
        PRIMARY KEY (agent_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_registrations (
        agent_id VARCHAR(36) NOT NULL,
        name TEXT NOT NULL,
        labels TEXT[] NOT NULL DEFAULT '{}',
        exports JSONB NOT NULL DEFAULT '[]',
        claim_sha256 VARCHAR(64) NOT NULL,
        registered_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        approved_at TIMESTAMP WITH TIME ZONE,
        PRIMARY KEY (agent_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_exports (
        agent_id VARCHAR(36) NOT NULL,
        name VARCHAR(64) NOT NULL,
        host_path TEXT NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'approved', 'refused')),
        announced_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        decided_at TIMESTAMP WITH TIME ZONE,
        decided_by TEXT,
        PRIMARY KEY (agent_id, name)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS project_sources (
        project VARCHAR(64) NOT NULL,
        agent_id VARCHAR(36) NOT NULL,
        export VARCHAR(64) NOT NULL,
        host_path TEXT NOT NULL,
        bound_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        bound_by TEXT,
        verified_at TIMESTAMP WITH TIME ZONE,
        PRIMARY KEY (project)
    );
    """,
)

INDEXES = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_registrations_claim
        ON agent_registrations (claim_sha256);
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS project_sources_export
        ON project_sources (agent_id, export);
    """,
)


def upgrade() -> None:
    """Create the tables."""
    for statement in (*TABLES, *INDEXES):
        op.execute(statement)


def downgrade() -> None:
    """Refuse: no revision drops a table."""
    raise RuntimeError("0008 holds enrolled agents: restore a backup instead")
