"""The model classes and the DDL of the revisions describe the same tables.

The DDL is what creates the schema and the classes are what code reads rows
with, so nothing but this test keeps the two from drifting apart.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

from sqlmodel import SQLModel

from enggraph.core import migrate, models
from enggraph.core.storage import ABOUT_COLUMNS, PROJECT_COLUMNS

CONSTRAINT_WORDS = ("PRIMARY", "CONSTRAINT", "UNIQUE", "CHECK")
FORBIDDEN = re.compile(r"\b(references|foreign\s+key|cascade)\b", re.IGNORECASE)


def revision(path: Path) -> ModuleType:
    """Load one revision from the file Alembic runs."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def revisions() -> list[ModuleType]:
    """Load every revision, in the order Alembic applies them."""
    paths = sorted((migrate.SCRIPTS / "versions").glob("[0-9]*.py"))
    return [revision(path) for path in paths]


def statements(*names: str) -> list[str]:
    """Return the DDL every revision keeps under these names."""
    return [
        statement
        for module in revisions()
        for name in names
        for statement in getattr(module, name, ())
    ]


def declared() -> dict[str, dict[str, bool]]:
    """Return table -> column -> nullable, read from the DDL."""
    tables: dict[str, dict[str, bool]] = {}
    for statement in statements("TABLES"):
        name, body = re.search(
            r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \((.*)\);", statement, re.DOTALL
        ).groups()
        keyed = re.search(r"^\s*PRIMARY KEY \(([^)]*)\)", body, re.MULTILINE)
        key = {part.strip() for part in keyed.group(1).split(",")} if keyed else set()
        columns: dict[str, bool] = {}
        for line in body.splitlines():
            words = line.split()
            if len(words) < 2 or words[0] in CONSTRAINT_WORDS:
                continue
            if not re.fullmatch(r"[a-z_0-9]+", words[0]) or not words[1].isupper():
                continue
            required = "NOT NULL" in line or "PRIMARY KEY" in line
            columns[words[0]] = not (required or words[0] in key)
        tables[name] = columns
    return tables


def test_every_table_has_a_model_and_every_model_a_table() -> None:
    """A table added to one side alone fails here."""
    assert set(SQLModel.metadata.tables) == set(declared())


def test_the_columns_agree_by_name_and_by_null() -> None:
    """Except the stand-in keys of `nodes` and `edges`, which are NOT NULL too."""
    for name, columns in declared().items():
        table = SQLModel.metadata.tables[name]
        assert {column.name: column.nullable for column in table.columns} == columns


def test_no_model_holds_a_key_to_another_table() -> None:
    """Tables are tied by plain value columns, never by a constraint."""
    for table in SQLModel.metadata.tables.values():
        assert not table.foreign_keys, table.name
    source = Path(models.__file__).read_text(encoding="utf-8")
    assert "Relationship(" not in source and "foreign_key=" not in source


def test_no_revision_holds_a_key_between_tables() -> None:
    """The same rule the pre-commit hook holds every revision to."""
    for statement in statements("TABLES", "INDEXES", "VIEWS"):
        assert not FORBIDDEN.search(statement), statement


def test_a_later_revision_only_creates() -> None:
    """New functionality is a new table: nothing after 0001 changes one."""
    for module in revisions()[1:]:
        for statement in (*module.TABLES, *getattr(module, "INDEXES", ())):
            assert statement.split()[0] == "CREATE", statement


def test_every_project_column_is_a_column_of_the_schema() -> None:
    """What a project drop walks must exist, or the drop fails mid-way."""
    tables = declared()
    for table, column in PROJECT_COLUMNS:
        assert column in tables[table], (table, column)


def test_every_about_column_is_a_column_of_the_schema() -> None:
    """What a project rename walks must exist, and a drop must not reach it."""
    tables = declared()
    for table, column in ABOUT_COLUMNS:
        assert column in tables[table], (table, column)
        assert table not in {name for name, _ in PROJECT_COLUMNS}
