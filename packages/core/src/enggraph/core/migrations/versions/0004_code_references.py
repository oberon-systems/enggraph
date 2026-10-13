"""What the code of a file refers to, kept unresolved for the linking pass.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Written by the code extractor a file at a time and read back by the pass
# that resolves imports, calls and bases, so an unchanged file is not parsed.
TABLES = (
    """
    CREATE TABLE IF NOT EXISTS code_references (
        project VARCHAR(64) NOT NULL,
        file_path TEXT NOT NULL,
        source_id VARCHAR(255) NOT NULL,
        kind VARCHAR(32) NOT NULL,
        name VARCHAR(255) NOT NULL,
        receiver VARCHAR(255) NOT NULL DEFAULT '',
        line INTEGER NOT NULL
    );
    """,
)

INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_code_references_file
    ON code_references (project, file_path);
    """,
)


def upgrade() -> None:
    """Create the table and its index."""
    for statement in (*TABLES, *INDEXES):
        op.execute(statement)


def downgrade() -> None:
    """Refuse: no revision drops a table."""
    raise RuntimeError("0004 holds what code refers to: restore a backup instead")
