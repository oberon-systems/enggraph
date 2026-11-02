"""Who may reach the stack: people, the tokens agents carry, browser sessions.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# A token and a session keep only the sha256 of their secret, never the secret.
TABLES = (
    """
    CREATE TABLE IF NOT EXISTS auth_principals (
        issuer VARCHAR(255) NOT NULL,
        subject VARCHAR(255) NOT NULL,
        display TEXT,
        role VARCHAR(16) NOT NULL DEFAULT 'reader'
            CHECK (role IN ('operator', 'reader')),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        last_login_at TIMESTAMP WITH TIME ZONE,
        PRIMARY KEY (issuer, subject)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_tokens (
        id VARCHAR(32) NOT NULL,
        secret_sha256 VARCHAR(64) NOT NULL,
        kind VARCHAR(16) NOT NULL CHECK (kind IN ('operator', 'mcp', 'api')),
        name TEXT NOT NULL,
        role VARCHAR(16) NOT NULL DEFAULT 'reader'
            CHECK (role IN ('operator', 'reader')),
        scopes TEXT[] NOT NULL DEFAULT '{}',
        projects TEXT[] NOT NULL DEFAULT '{}',
        host VARCHAR(255),
        created_by TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP WITH TIME ZONE,
        last_used_at TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        id_sha256 VARCHAR(64) NOT NULL,
        issuer VARCHAR(255) NOT NULL,
        subject VARCHAR(255) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        last_seen_at TIMESTAMP WITH TIME ZONE,
        PRIMARY KEY (id_sha256)
    );
    """,
)

INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS auth_sessions_principal
        ON auth_sessions (issuer, subject);
    """,
)


def upgrade() -> None:
    """Create the tables."""
    for statement in (*TABLES, *INDEXES):
        op.execute(statement)


def downgrade() -> None:
    """Refuse: no revision drops a table."""
    raise RuntimeError("0007 holds tokens and sessions: restore a backup instead")
