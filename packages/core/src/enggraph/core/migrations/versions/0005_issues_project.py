"""The built-in project holding issues, beside the memories and suggestions.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# An issue is a node of `_issues`, as a suggestion is one of `_suggestions`,
# so the revision adds the project row and no table.
TABLES: tuple[str, ...] = ()

INDEXES: tuple[str, ...] = ()

ROWS = (
    """
    INSERT INTO projects (name, root_path, type)
    VALUES ('_issues', 'issues://agent', 'issues')
    ON CONFLICT (name) DO NOTHING;
    """,
)


def upgrade() -> None:
    """Add the project row."""
    for statement in (*TABLES, *INDEXES, *ROWS):
        op.execute(statement)


def downgrade() -> None:
    """Refuse: going down would orphan every issue written."""
    raise RuntimeError("0005 holds the issues project: restore a backup instead")
