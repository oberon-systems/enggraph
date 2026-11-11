"""Which database a migration run accepts, and what it does to each.

Nothing here reaches a database: the cursor answers the questions
`owner` asks, and the revisions are read from disk the way Alembic reads them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from psycopg2 import sql

from enggraph.core import migrate


class Catalog:
    """A cursor over a database holding the named tables."""

    def __init__(self, tables: set[str], stamped: int = 1) -> None:
        """Take the tables and Alembic's row count."""
        self.tables = tables
        self.stamped = stamped
        self.answer: tuple[Any, ...] | None = None

    def execute(self, statement: str, params: tuple[Any, ...] = ()) -> None:
        """Answer one of the statements `owner` sends."""
        if "to_regclass" in statement:
            self.answer = (params[0] in self.tables,)
        else:
            self.answer = (self.stamped,)

    def fetchone(self) -> tuple[Any, ...] | None:
        """Return the answer to the last statement."""
        return self.answer


def test_an_empty_database_is_fresh() -> None:
    """Nothing to mark: revision 0001 creates everything."""
    assert migrate.owner(Catalog(set())) == migrate.FRESH


def test_a_database_the_last_0x_release_left_is_marked_not_rebuilt() -> None:
    """Release 0.24.0 left exactly what 0001 creates."""
    catalog = Catalog({"schema_migrations", "projects", "chunks"})
    assert migrate.owner(catalog) == migrate.UNMARKED


def test_a_database_an_older_release_left_is_refused() -> None:
    """The steps in between exist only in the 0.x releases."""
    catalog = Catalog({"schema_migrations", "projects"})
    with pytest.raises(migrate.Refused, match="v0.24.0"):
        migrate.owner(catalog)


def test_a_marked_database_stays_with_alembic() -> None:
    """Whether or not the old version table is still there."""
    tables = {"alembic_version", "projects", "chunks"}
    assert migrate.owner(Catalog(tables)) == migrate.ALEMBIC
    assert migrate.owner(Catalog(tables | {"schema_migrations"})) == migrate.ALEMBIC


def test_a_mark_that_was_never_written_is_made_again() -> None:
    """A run that died after creating the version table is not a new owner."""
    catalog = Catalog({"alembic_version", "projects", "chunks"}, stamped=0)
    assert migrate.owner(catalog) == migrate.UNMARKED


def test_an_older_backup_restored_under_the_mark_is_refused() -> None:
    """A restore replaces the tables and leaves the version table behind."""
    catalog = Catalog({"alembic_version", "schema_migrations", "projects"})
    with pytest.raises(migrate.Refused, match="older than"):
        migrate.owner(catalog)


def test_only_an_unmarked_database_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mark on a fresh database would skip the revision that creates it."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migrate.command, "stamp", lambda config, rev: calls.append(("stamp", rev))
    )
    monkeypatch.setattr(
        migrate.command, "upgrade", lambda config, rev: calls.append(("up", rev))
    )
    monkeypatch.setattr(migrate, "forget_0x", lambda: None)

    monkeypatch.setattr(migrate, "found", lambda: migrate.FRESH)
    migrate.up()
    monkeypatch.setattr(migrate, "found", lambda: migrate.UNMARKED)
    migrate.up()
    assert calls == [("up", "head"), ("stamp", "0001"), ("up", "head")]


class Marked:
    """A cursor over a database marked with a revision and holding some tables."""

    def __init__(self, revision: str, tables: set[str]) -> None:
        """Take the mark and the tables."""
        self.revision = revision
        self.tables = tables
        self.answer: tuple[Any, ...] | None = None

    def execute(self, statement: str, params: tuple[Any, ...] = ()) -> None:
        """Answer with the mark, or whether a table is there."""
        if "to_regclass" in statement:
            self.answer = (params[0] in self.tables,)
        else:
            self.answer = (self.revision,)

    def fetchone(self) -> tuple[Any, ...] | None:
        """Return the answer to the last statement."""
        return self.answer


def test_a_mark_ahead_of_its_tables_goes_back_one_revision() -> None:
    """So the revision runs again and adds what is missing, dropping nothing."""
    scripts = ScriptDirectory.from_config(migrate.config())
    whole = {"prompts", "roadmaps", "roadmap_items"}
    assert migrate.unfinished(Marked("0002", whole), scripts) is None
    assert migrate.unfinished(Marked("0002", {"prompts"}), scripts) == "0001"
    assert migrate.unfinished(Marked("0001", set()), scripts) is None


def test_a_marked_database_missing_a_table_has_the_revision_run_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marked back, then upgraded: no statement is written by hand."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migrate.command, "stamp", lambda config, rev: calls.append(("stamp", rev))
    )
    monkeypatch.setattr(
        migrate.command, "upgrade", lambda config, rev: calls.append(("up", rev))
    )
    monkeypatch.setattr(migrate, "forget_0x", lambda: None)
    monkeypatch.setattr(migrate, "found", lambda: migrate.ALEMBIC)

    monkeypatch.setattr(migrate, "behind", lambda: None)
    migrate.up()
    monkeypatch.setattr(migrate, "behind", lambda: "0001")
    migrate.up()
    assert calls == [("up", "head"), ("stamp", "0001"), ("up", "head")]


def test_a_later_revision_can_be_run_again() -> None:
    """Which is what going back one revision relies on."""
    scripts = ScriptDirectory.from_config(migrate.config())
    for script in scripts.walk_revisions():
        if script.revision == migrate.BASELINE:
            continue
        for statement in (*script.module.TABLES, *script.module.INDEXES):
            assert "IF NOT EXISTS" in statement, statement


def test_every_run_drops_the_old_version_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database marked before the table was dropped loses it as well."""
    calls: list[str] = []
    monkeypatch.setattr(migrate.command, "upgrade", lambda config, rev: None)
    monkeypatch.setattr(migrate, "found", lambda: migrate.ALEMBIC)
    monkeypatch.setattr(migrate, "behind", lambda: None)
    monkeypatch.setattr(migrate, "forget_0x", lambda: calls.append("forget"))
    migrate.up()
    assert calls == ["forget"]


def test_a_refusal_ends_the_run_with_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So the services waiting on the migration never start."""

    def refuse() -> str:
        raise migrate.Refused("too old")

    monkeypatch.setattr(migrate, "found", refuse)
    assert migrate.main(["up"]) == 1


def test_the_revisions_form_one_chain_from_the_baseline() -> None:
    """Two heads would leave `upgrade head` with nothing to choose."""
    scripts = ScriptDirectory.from_config(migrate.config())
    assert len(scripts.get_heads()) == 1
    assert scripts.get_base() == migrate.BASELINE


class Roles:
    """A cursor that keeps what it was sent, over a cluster holding some roles."""

    def __init__(self, me: str, roles: set[str]) -> None:
        """Take the connected role and the roles there are."""
        self.me = me
        self.roles = roles
        self.sent: list[str] = []
        self.answer: tuple[Any, ...] | None = None

    def execute(
        self, statement: str | sql.Composable, params: tuple[Any, ...] = ()
    ) -> None:
        """Keep a statement, and answer the two questions `grant` asks."""
        text = statement if isinstance(statement, str) else render(statement)
        self.sent.append(text)
        if "current_user" in text:
            self.answer = (self.me,)
        elif "pg_roles" in text:
            self.answer = (1,) if params[0] in self.roles else None

    def fetchone(self) -> tuple[Any, ...] | None:
        """Return the answer to the last question."""
        return self.answer


def render(statement: sql.Composable) -> str:
    """Spell a composed statement without a connection to quote it for."""
    parts = getattr(statement, "seq", [statement])
    return "".join(
        f'"{part.strings[0]}"' if hasattr(part, "strings") else part.string
        for part in parts
    )


def test_the_services_role_is_created_plain_and_granted_rows_alone() -> None:
    """It reads and writes rows; it creates, alters and drops nothing."""
    cursor = Roles("owner", {"owner"})
    migrate.grant(cursor, "alpha", "a-password")
    made = next(one for one in cursor.sent if "ROLE" in one)
    assert made.startswith('CREATE ROLE "alpha" LOGIN NOSUPERUSER NOCREATEDB')
    granted = " ".join(one for one in cursor.sent if "GRANT" in one)
    assert "SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" in granted
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in granted
    for power in ("ALL PRIVILEGES", "CREATE ON", "TRUNCATE", "SUPERUSER ON"):
        assert power not in granted


def test_a_role_already_there_is_reset_not_created() -> None:
    """A role someone made a superuser by hand loses that at the next run."""
    cursor = Roles("owner", {"owner", "alpha"})
    migrate.grant(cursor, "alpha", "a-password")
    made = next(one for one in cursor.sent if "ROLE" in one)
    assert made.startswith('ALTER ROLE "alpha" LOGIN NOSUPERUSER')


def test_the_owner_of_the_schema_is_not_the_services_role() -> None:
    """Handing the services the migrating role would undo all of it."""
    with pytest.raises(migrate.Refused, match="another role"):
        migrate.grant(Roles("owner", {"owner"}), "owner", "a-password")


def test_a_services_role_without_a_password_is_refused() -> None:
    """An empty password would be a role anybody on the network logs in as."""
    with pytest.raises(migrate.Refused, match="SERVICE_DATABASE_PASSWORD"):
        migrate.grant(Roles("owner", {"owner"}), "alpha", "")


def test_only_the_one_shot_jobs_hold_the_superuser() -> None:
    """Every service that stays up connects as the role without DDL."""
    text = (Path(__file__).resolve().parents[3] / "docker-compose.yaml").read_text()
    body = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    services = re.split(r"(?m)^  (?=[a-z][a-z0-9-]*:$)", body)[1:]
    holders = {
        one.split(":", 1)[0]
        for one in services
        if "POSTGRES_PASSWORD" in one or "*owner-url" in one
    }
    assert holders == {"postgres", "migrate", "rauthy-db"}
