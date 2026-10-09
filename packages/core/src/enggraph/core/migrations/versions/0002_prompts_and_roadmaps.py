"""Execution prompts, and roadmaps with their items, each tied to a plan by id.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# `about` is a project name, NULL for a global record. A prompt and an item
# name a plan by id; an empty `plan_id` on an item is an item with no plan.
# IF NOT EXISTS because the migrate run applies a revision again when a
# database is marked with it and lacks one of its tables.
TABLES = (
    """
    CREATE TABLE IF NOT EXISTS prompts (
        id VARCHAR(255) NOT NULL PRIMARY KEY,
        plan_id VARCHAR(255) NOT NULL,
        about VARCHAR(64),
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        status VARCHAR(50) NOT NULL DEFAULT 'active',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS roadmaps (
        id VARCHAR(255) NOT NULL PRIMARY KEY,
        about VARCHAR(64),
        title TEXT NOT NULL,
        content TEXT,
        status VARCHAR(50) NOT NULL DEFAULT 'active',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS roadmap_items (
        roadmap_id VARCHAR(255) NOT NULL,
        id VARCHAR(255) NOT NULL,
        position INTEGER NOT NULL,
        section VARCHAR(255) NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        content TEXT,
        status VARCHAR(50) NOT NULL DEFAULT 'open',
        plan_id VARCHAR(255) NOT NULL DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (roadmap_id, id)
    );
    """,
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_prompts_plan ON prompts (plan_id);",
    "CREATE INDEX IF NOT EXISTS idx_prompts_about ON prompts (about, status);",
    "CREATE INDEX IF NOT EXISTS idx_roadmaps_about ON roadmaps (about, status);",
    """
    CREATE INDEX IF NOT EXISTS idx_roadmap_items_order
    ON roadmap_items (roadmap_id, position);
    """,
    "CREATE INDEX IF NOT EXISTS idx_roadmap_items_plan ON roadmap_items (plan_id);",
)


def upgrade() -> None:
    """Create the three tables and their indexes."""
    for statement in (*TABLES, *INDEXES):
        op.execute(statement)


def downgrade() -> None:
    """Refuse: going down would drop every prompt and roadmap written."""
    raise RuntimeError("0002 holds prompts and roadmaps: restore a backup instead")
