"""Bring the schema to the last revision, whatever owned it before.

Alembic owns the schema from revision 0001, which is the schema release
0.24.0 leaves. A database that release brought that far is marked as 0001
and gains nothing; an older one is refused, because the steps in between
exist only in the 0.x releases.

A mark is held to the tables: a database marked with a later revision and
lacking a table that revision creates has it applied again, which adds what
is missing and touches nothing that is there.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg2.extensions import cursor as Cursor

from enggraph.core.storage import get_db_connection

LOG = logging.getLogger(__name__)

SCRIPTS = Path(__file__).parent / "migrations"
BASELINE = "0001"
LAST_0X = "0.24.0"
# The last migration of 0.24.0 created this table: with it, the schema is 0001.
MARK_OF_0X = "chunks"
# Where the 0.x releases kept their migration numbers; nothing reads it now.
RECORD_OF_0X = "schema_migrations"

CREATED = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)")

FRESH = "fresh"
UNMARKED = "unmarked"
ALEMBIC = "alembic"

TOO_OLD = (
    f"this database holds a schema older than the one {LAST_0X} leaves, and "
    f"this release starts there. Check out v{LAST_0X}, run `make db migrate` "
    "under it, then upgrade to this one. See docs/breaking-changes.md."
)


class Refused(RuntimeError):
    """The database is in a state no revision starts from."""


def config() -> Config:
    """Return the Alembic configuration, with no ini file behind it."""
    settled = Config()
    settled.set_main_option("script_location", str(SCRIPTS))
    return settled


def exists(cursor: Cursor, table: str) -> bool:
    """Say whether a table is there."""
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL;", (table,))
    row = cursor.fetchone()
    return bool(row and row[0])


def owner(cursor: Cursor) -> str:
    """Say who brought the schema to where it is, or refuse."""
    # Asked even of a marked database: restoring an older backup brings back
    # the old tables and leaves the mark of the newer ones in place.
    if exists(cursor, "projects") and not exists(cursor, MARK_OF_0X):
        raise Refused(TOO_OLD)
    if exists(cursor, "alembic_version"):
        # Empty when a first run died between creating it and writing to it.
        cursor.execute("SELECT count(*) FROM alembic_version;")
        row = cursor.fetchone()
        if row and row[0]:
            return ALEMBIC
    if exists(cursor, MARK_OF_0X):
        return UNMARKED
    return FRESH


def found() -> str:
    """Read the owner over a connection of its own."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            return owner(cursor)
    finally:
        conn.close()


def unfinished(cursor: Cursor, scripts: ScriptDirectory) -> str | None:
    """Return the revision to go back to, when a mark is ahead of the tables.

    That is the one before the first revision after the baseline which the
    database is marked with and one of whose tables is not there.
    """
    cursor.execute("SELECT version_num FROM alembic_version;")
    row = cursor.fetchone()
    applied = list(scripts.walk_revisions("base", str(row[0]))) if row else []
    for script in reversed(applied):
        if script.revision == BASELINE:
            continue
        tables = CREATED.findall(" ".join(getattr(script.module, "TABLES", ())))
        if not all(exists(cursor, table) for table in tables):
            return str(script.down_revision)
    return None


def behind() -> str | None:
    """Ask `unfinished` over a connection of its own."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            return unfinished(cursor, ScriptDirectory.from_config(config()))
    finally:
        conn.close()


def forget_0x() -> None:
    """Drop the table the 0.x releases kept their migration numbers in."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {RECORD_OF_0X};")
        conn.commit()
    finally:
        conn.close()


def up() -> None:
    """Apply every pending revision."""
    settled = config()
    state = found()
    if state == UNMARKED:
        LOG.info("Schema is the one %s left: marking it as %s", LAST_0X, BASELINE)
        command.stamp(settled, BASELINE)
    elif state == ALEMBIC:
        back = behind()
        if back is not None:
            LOG.info("A table of a marked revision is missing: back to %s", back)
            command.stamp(settled, back)
    command.upgrade(settled, "head")
    forget_0x()


def down() -> None:
    """Undo the last applied revision."""
    command.downgrade(config(), "-1")


def status() -> None:
    """Print the revision the database is at, and the ones there are."""
    settled = config()
    state = found()
    if state != ALEMBIC:
        print(f"not migrated yet ({state})")
    else:
        command.current(settled)
    command.history(settled)


def validate() -> None:
    """Check the revisions form one chain, without touching the database."""
    heads = ScriptDirectory.from_config(config()).get_heads()
    if len(heads) != 1:
        raise Refused(f"expected one head revision, found {sorted(heads)}")
    print(f"revisions are valid, head is {heads[0]}")


COMMANDS = {"up": up, "down": down, "status": status, "validate": validate}


def main(argv: list[str] | None = None) -> int:
    """Run one command; `up` when none is named."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = sys.argv[1:] if argv is None else argv
    name = args[0] if args else "up"
    if name not in COMMANDS:
        print(f"unknown command {name}: one of {', '.join(COMMANDS)}")
        return 2
    try:
        COMMANDS[name]()
    except Refused as refused:
        LOG.error("%s", refused)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
