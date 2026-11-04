"""The master keys host agents' credentials derive from.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLES = (
    """
    CREATE TABLE IF NOT EXISTS agent_master_keys (
        kid VARCHAR(64) NOT NULL,
        key TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (kid)
    );
    """,
)

INDEXES = ()


def upgrade() -> None:
    """Create the table."""
    for statement in TABLES:
        op.execute(statement)


def downgrade() -> None:
    """Refuse: no revision drops a table."""
    raise RuntimeError("0009 holds the master keys: restore a backup instead")
