"""The revision of the parser that last read a file, apart from its content hash.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# `indexed_files.hash` used to mix every parser's source into each file's hash,
# so a change to one parser reparsed and re-embedded every file of every tree.
TABLES = (
    """
    CREATE TABLE IF NOT EXISTS parsed_files (
        project VARCHAR(64) NOT NULL,
        file_path TEXT NOT NULL,
        revision VARCHAR(16) NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (project, file_path)
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
    raise RuntimeError("0006 holds parser revisions: restore a backup instead")
