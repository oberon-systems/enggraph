"""The model classes and the DDL of revision 0001 describe the same tables.

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
from enggraph.core.storage import PROJECT_COLUMNS

CONSTRAINT_WORDS = ("PRIMARY", "CONSTRAINT", "UNIQUE", "CHECK")
FORBIDDEN = re.compile(r"\b(references|foreign\s+key|cascade)\b", re.IGNORECASE)


def baseline() -> ModuleType:
    """Load revision 0001 from the file Alembic runs."""
    path = migrate.SCRIPTS / "versions" / "0001_schema.py"
    spec = importlib.util.spec_from_file_location("baseline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declared() -> dict[str, dict[str, bool]]:
    """Return table -> column -> nullable, read from the DDL."""
    tables: dict[str, dict[str, bool]] = {}
    for statement in baseline().TABLES:
        name, body = re.search(
            r"CREATE TABLE (\w+) \((.*)\);", statement, re.DOTALL
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


def test_the_baseline_holds_no_key_between_tables() -> None:
    """The same rule the pre-commit hook holds every later revision to."""
    module = baseline()
    for statement in (*module.TABLES, *module.INDEXES, *module.VIEWS):
        assert not FORBIDDEN.search(statement), statement


def test_every_project_column_is_a_column_of_the_schema() -> None:
    """What a project drop walks must exist, or the drop fails mid-way."""
    tables = declared()
    for table, column in PROJECT_COLUMNS:
        assert column in tables[table], (table, column)
