"""The indexer's SQL, run against a migrated schema.

Skipped unless EVAL_DATABASE_URL names a throwaway database (`make eval-up`).
Every statement is prepared, and the embedding queue is driven end to end
inside a transaction that is rolled back.
"""

from __future__ import annotations

import ast
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor

from enggraph import embedjobs, storage
from enggraph.config import EMBED_DIM

DATABASE_URL = os.environ.get("EVAL_DATABASE_URL")
SOURCES = Path(__file__).resolve().parent.parent / "src" / "enggraph"
STATEMENT = re.compile(r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE)\s")
PLACEHOLDER = re.compile(r"%\((\w+)\)s|%s|%%")
# Values are inlined by psycopg2, so a bare placeholder can be untypeable for
# PREPARE while being fine at runtime; those are skipped, not failed.
UNTYPEABLE = {"42P18", "42725"}

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(DATABASE_URL is None, reason="EVAL_DATABASE_URL is not set"),
]


def statements() -> list[tuple[str, str]]:
    """Every literal SQL statement in the package, as (location, text)."""
    found = []
    for path in sorted(SOURCES.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # An f-string fragment is half a statement; only whole literals count.
        fragments = {
            id(part)
            for joined in ast.walk(tree)
            if isinstance(joined, ast.JoinedStr)
            for part in joined.values
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and id(node) not in fragments
                and isinstance(node.value, str)
                and STATEMENT.match(node.value)
            ):
                found.append((f"{path.name}:{node.lineno}", node.value))
    return found


def numbered(sql: str) -> str:
    """Rewrite psycopg2 placeholders into the $n form PREPARE takes."""
    names: dict[str, int] = {}
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(0) == "%%":
            return "%"
        name = match.group(1)
        if name is not None and name in names:
            return f"${names[name]}"
        count += 1
        if name is not None:
            names[name] = count
        return f"${count}"

    return PLACEHOLDER.sub(replace, sql).rstrip().rstrip(";")


@pytest.fixture(scope="module")
def connection() -> Iterator[Connection]:
    """One connection to the eval database for the module."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def cursor(connection: Connection) -> Iterator[Cursor]:
    """Yield a cursor, rolling back and deallocating afterwards."""
    cur = connection.cursor()
    try:
        yield cur
    finally:
        connection.rollback()
        cur.execute("DEALLOCATE ALL;")
        connection.rollback()
        cur.close()


def test_statements_are_found() -> None:
    """The collector sees the package's SQL at all."""
    assert len(statements()) > 30


@pytest.mark.parametrize(("where", "sql"), statements())
def test_statement_prepares(cursor: Cursor, where: str, sql: str) -> None:
    """Every literal statement parses and resolves against the schema."""
    try:
        cursor.execute(f"PREPARE checked AS {numbered(sql)}")
    except psycopg2.Error as error:
        if error.pgcode in UNTYPEABLE:
            pytest.skip(f"{where}: placeholder types need values")
        raise AssertionError(f"{where}: {error}") from error


def test_queue_reaches_full_coverage(cursor: Cursor) -> None:
    """Every file is queued, embedded and counted, hashed or not."""
    project = f"alpha-{uuid.uuid4().hex[:8]}"
    storage.ensure_project(cursor, project, f"/code/{project}")
    storage.upsert_file_node(cursor, project, "src/hashed.py", "hashed")
    storage.upsert_file_hash(cursor, project, "src/hashed.py", "abc")
    # No hash: the case the queue once could never pick up.
    storage.upsert_file_node(cursor, project, "src/unhashed.py", "unhashed")

    assert embedjobs.enqueue_project(cursor, project, "model-a") == 2
    assert embedjobs.enqueue_project(cursor, project, "model-a") == 0

    tasks = embedjobs.claim(cursor, [project], 10, 60)
    assert sorted(task["file_path"] for task in tasks) == [
        "src/hashed.py",
        "src/unhashed.py",
    ]
    assert embedjobs.queue_depth(cursor, project)["running"] == 2

    for task in tasks:
        storage.replace_file_embeddings(
            cursor,
            project,
            task["file_path"],
            task["content_hash"],
            "model-a",
            [(0, 1, 3, "body", [0.1] * EMBED_DIM)],
        )
        embedjobs.finish(cursor, task["id"])

    coverage = storage.embedding_coverage(cursor, project)
    assert coverage["files"] == coverage["indexed_files"] == 2
    assert coverage["chunks"] == 2
    assert embedjobs.queue_depth(cursor, project)["done"] == 2
    assert embedjobs.enqueue_project(cursor, project, "model-a") == 0


@pytest.mark.xfail(
    strict=True,
    reason="enqueue_project only resets a task when the hash moved, not the model",
)
def test_model_switch_requeues(cursor: Cursor) -> None:
    """A new model makes finished files work again."""
    project = f"alpha-{uuid.uuid4().hex[:8]}"
    storage.ensure_project(cursor, project, f"/code/{project}")
    storage.upsert_file_node(cursor, project, "src/a.py", "a")
    embedjobs.enqueue_project(cursor, project, "model-a")
    for task in embedjobs.claim(cursor, [project], 10, 60):
        embedjobs.finish(cursor, task["id"])

    assert embedjobs.enqueue_project(cursor, project, "model-b") == 1


def test_failure_sets_the_skip_bit(cursor: Cursor) -> None:
    """A spent file is skipped until retried."""
    project = f"alpha-{uuid.uuid4().hex[:8]}"
    storage.ensure_project(cursor, project, f"/code/{project}")
    storage.upsert_file_node(cursor, project, "src/bad.py", "bad")
    embedjobs.enqueue_project(cursor, project, "model-a")

    (task,) = embedjobs.claim(cursor, [project], 10, 60)
    embedjobs.fail(cursor, task["id"], "boom", max_attempts=1)

    assert embedjobs.queue_depth(cursor, project)["failed"] == 1
    assert storage.embedding_coverage(cursor, project)["skipped"] == 1
    assert embedjobs.enqueue_project(cursor, project, "model-a") == 0
    assert embedjobs.retry_failed(cursor, project) == 1
    assert embedjobs.queue_depth(cursor, project)["pending"] == 1
