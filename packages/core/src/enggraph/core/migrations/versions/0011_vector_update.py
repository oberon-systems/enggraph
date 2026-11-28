"""Bring the vector extension to the version the image ships.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLES: tuple[str, ...] = ()

INDEXES: tuple[str, ...] = ()

# A new image replaces the extension's files, never the version a database
# already created; pgvector 0.8.7 came with the pg16 image of 2026-11.
EXTENSIONS = ("ALTER EXTENSION vector UPDATE;",)


def upgrade() -> None:
    """Update the extension."""
    for statement in EXTENSIONS:
        op.execute(statement)


def downgrade() -> None:
    """Refuse: pgvector ships no script back to an older version."""
    raise RuntimeError("0011 updated pgvector: restore a backup instead")
