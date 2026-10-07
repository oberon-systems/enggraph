"""Run the revisions over the connection every other module opens."""

from alembic import context
from sqlmodel import create_engine

from enggraph.core.storage import get_db_connection


def run() -> None:
    """Apply what the command asked for, in one transaction."""
    # The URL is libpq's to read, as everywhere else: SQLAlchemy is handed
    # the connection, never the address.
    engine = create_engine("postgresql+psycopg2://", creator=get_db_connection)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run()
