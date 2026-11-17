"""The revision of the code that last linked a project's edges.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# A fix to a resolver changes no file and no parser, so without this nothing
# told an incremental run to draw a project's edges again.
TABLES = (
    """
    CREATE TABLE IF NOT EXISTS link_revisions (
        project VARCHAR(64) NOT NULL,
        revision VARCHAR(16) NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (project)
    );
    """,
)

INDEXES: tuple[str, ...] = ()


def upgrade() -> None:
    """Create the table."""
    for statement in (*TABLES, *INDEXES):
        op.execute(statement)


def downgrade() -> None:
    """Refuse: no revision drops a table."""
    raise RuntimeError("0010 holds link revisions: restore a backup instead")
