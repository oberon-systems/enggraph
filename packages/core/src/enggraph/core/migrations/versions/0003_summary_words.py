"""The words of node summaries, kept so a search reads an index and parses nothing.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Both start empty and are filled by the scheduler, a project at a time: a
# project named in `summary_words_filled` has every row it should have.
TABLES = (
    """
    CREATE TABLE IF NOT EXISTS summary_words (
        project VARCHAR(64) NOT NULL,
        node_id VARCHAR(255) NOT NULL,
        words TSVECTOR NOT NULL,
        summary_hash VARCHAR(32) NOT NULL,
        PRIMARY KEY (project, node_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS summary_words_filled (
        project VARCHAR(64) NOT NULL PRIMARY KEY,
        filled_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """,
)

INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_summary_words_words
    ON summary_words USING gin (words);
    """,
)


def upgrade() -> None:
    """Create the two tables and the index."""
    for statement in (*TABLES, *INDEXES):
        op.execute(statement)


def downgrade() -> None:
    """Refuse: no revision drops a table."""
    raise RuntimeError("0003 holds summary words: restore a backup instead")
