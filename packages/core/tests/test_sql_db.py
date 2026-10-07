"""The indexer's SQL, run against a migrated schema.

Skipped unless EVAL_DATABASE_URL names a throwaway database (`make eval-up`).
Every statement is prepared, and the embedding queue is driven end to end
inside a transaction that is rolled back, over the in-process Valkey.
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

from enggraph.core import embedjobs, storage
from enggraph.core.config import EMBED_DIM

DATABASE_URL = os.environ.get("EVAL_DATABASE_URL")
PACKAGES = Path(__file__).resolve().parents[2]
BACKUP = PACKAGES.parent / "scripts" / "backup.sh"
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
    for path in sorted(PACKAGES.glob("*/src/**/*.py")):
        # A revision is run once by the migration, not prepared by a service.
        if "migrations" in path.parts:
            continue
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

    tasks = embedjobs.claim([project], 10, 60)
    assert sorted(task["file_path"] for task in tasks) == [
        "src/hashed.py",
        "src/unhashed.py",
    ]
    assert embedjobs.queue_depth(project)["running"] == 2

    for task in tasks:
        storage.replace_file_embeddings(
            cursor,
            project,
            task["file_path"],
            task["content_hash"],
            "model-a",
            [(0, 1, 3, "body", [0.1] * EMBED_DIM)],
        )
        embedjobs.finish(task)

    coverage = storage.embedding_coverage(cursor, "model-a", {})[project]
    assert coverage["files"] == coverage["indexed_files"] == 2
    assert coverage["chunks"] == 2
    assert embedjobs.queue_depth(project)["done"] == 2
    assert embedjobs.enqueue_project(cursor, project, "model-a") == 0


def test_model_switch_requeues(cursor: Cursor) -> None:
    """A new model makes finished files work again."""
    project = f"alpha-{uuid.uuid4().hex[:8]}"
    storage.ensure_project(cursor, project, f"/code/{project}")
    storage.upsert_file_node(cursor, project, "src/a.py", "a")
    embedjobs.enqueue_project(cursor, project, "model-a")
    for task in embedjobs.claim([project], 10, 60):
        embedjobs.finish(task)

    assert embedjobs.enqueue_project(cursor, project, "model-b") == 1


def test_chunker_revision_requeues(cursor: Cursor) -> None:
    """Chunks cut by an older chunker make the file work again."""
    project = f"alpha-{uuid.uuid4().hex[:8]}"
    storage.ensure_project(cursor, project, f"/code/{project}")
    storage.upsert_file_node(cursor, project, "src/a.py", "a")
    embedjobs.enqueue_project(cursor, project, "model-a")
    for task in embedjobs.claim([project], 10, 60):
        storage.replace_file_embeddings(
            cursor,
            project,
            task["file_path"],
            task["content_hash"],
            "model-a",
            [(0, 1, 3, "body", [0.1] * EMBED_DIM)],
        )
        embedjobs.finish(task)
    assert embedjobs.enqueue_project(cursor, project, "model-a") == 0

    cursor.execute(
        "UPDATE chunks SET chunker = chunker - 1 WHERE project = %s;",
        (project,),
    )
    assert embedjobs.enqueue_project(cursor, project, "model-a") == 1


def test_failure_sets_the_skip_bit(cursor: Cursor) -> None:
    """A spent file is skipped until retried."""
    project = f"alpha-{uuid.uuid4().hex[:8]}"
    storage.ensure_project(cursor, project, f"/code/{project}")
    storage.upsert_file_node(cursor, project, "src/bad.py", "bad")
    embedjobs.enqueue_project(cursor, project, "model-a")

    (task,) = embedjobs.claim([project], 10, 60)
    embedjobs.fail(cursor, task, "boom", max_attempts=1)

    assert embedjobs.queue_depth(project)["failed"] == 1
    coverage = storage.embedding_coverage(cursor, "model-a", {})
    assert coverage[project]["skipped"] == 1
    assert embedjobs.enqueue_project(cursor, project, "model-a") == 0
    assert embedjobs.retry_failed(cursor, project) == 1
    assert embedjobs.enqueue_project(cursor, project, "model-a") == 1
    assert embedjobs.queue_depth(project)["pending"] == 1


def test_project_links_join_one_provider_only(cursor: Cursor) -> None:
    """One other provider links a name; a manual export outlives a run."""
    suffix = uuid.uuid4().hex[:8]
    alpha, beta, gamma = (f"{name}-{suffix}" for name in ("alpha", "beta", "gamma"))
    for project in (alpha, beta, gamma):
        storage.ensure_project(cursor, project, f"/code/{project}")

    storage.replace_project_links(
        cursor,
        alpha,
        [("image", f"worker-{suffix}", "worker/"), ("npm", f"api-{suffix}", "./")],
        [],
    )
    cursor.execute(
        "INSERT INTO provided_names (project, kind, name, node_id, origin) "
        "VALUES (%s, 'image', %s, './', 'manual');",
        (alpha, f"built-by-ci-{suffix}"),
    )
    storage.replace_project_links(cursor, gamma, [("npm", f"api-{suffix}", "./")], [])
    storage.replace_project_links(
        cursor,
        beta,
        [],
        [
            ("image", f"worker-{suffix}", "compose.yml::service.job", "uses_image"),
            ("image", f"built-by-ci-{suffix}", "compose.yml::service.ci", "uses_image"),
            ("npm", f"api-{suffix}", "package.json", "depends_on"),
        ],
    )
    # A second run of alpha must keep the export declared by hand.
    storage.replace_project_links(
        cursor,
        alpha,
        [("image", f"worker-{suffix}", "worker/"), ("npm", f"api-{suffix}", "./")],
        [],
    )

    cursor.execute(
        "SELECT name, target_project, target_id FROM project_links "
        "WHERE source_project = %s ORDER BY name;",
        (beta,),
    )
    assert cursor.fetchall() == [
        (f"built-by-ci-{suffix}", alpha, "./"),
        (f"worker-{suffix}", alpha, "worker/"),
    ]
    assert storage.count_project_links(cursor, beta) == (2, 1)

    storage.replace_project_links(cursor, alpha, [("npm", f"api-{suffix}", "./")], [])
    assert storage.count_project_links(cursor, beta) == (1, 1)


def test_no_table_holds_a_foreign_key(cursor: Cursor) -> None:
    """Tables refer to each other by value; the code does what a cascade did."""
    cursor.execute(
        "SELECT table_name, constraint_name FROM information_schema.table_constraints"
        " WHERE constraint_type = 'FOREIGN KEY' AND table_schema = current_schema();"
    )
    assert cursor.fetchall() == []


def test_every_table_naming_a_project_is_dropped(cursor: Cursor) -> None:
    """A table left out of the drop would keep rows of a project that is gone."""
    cursor.execute(
        "SELECT DISTINCT c.table_name, c.column_name"
        "  FROM information_schema.columns AS c"
        "  JOIN information_schema.tables AS t USING (table_schema, table_name)"
        " WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'"
        "   AND c.column_name IN"
        "       ('project', 'organization', 'source_project', 'target_project');"
    )
    found = {(str(table), str(column)) for table, column in cursor.fetchall()}
    kept = {("projects", "project"), ("index_jobs", "project")}
    assert found - kept == set(storage.PROJECT_COLUMNS)
    restore = BACKUP.read_text(encoding="utf-8")
    missing = [t for t in storage.PROJECT_TABLES if f"DELETE FROM {t} " not in restore]
    assert missing == []


def test_a_drop_leaves_no_row_behind(cursor: Cursor) -> None:
    """Every row the project had goes with it, and nothing of another project."""
    alpha, beta = (f"{name}-{uuid.uuid4().hex[:8]}" for name in ("alpha", "beta"))
    for project in (alpha, beta):
        storage.ensure_project(cursor, project, f"/code/{project}")
        storage.upsert_file_node(cursor, project, "src/a.py", "a")
        storage.upsert_file_hash(cursor, project, "src/a.py", "abc")
        storage.replace_file_embeddings(
            cursor,
            project,
            "src/a.py",
            "abc",
            "model-a",
            [(0, 1, 3, "body", [0.1] * EMBED_DIM)],
        )
    storage.write_ignore(cursor, alpha, "*.log")
    cursor.execute(
        "INSERT INTO declared_links (source_project, target_project, relation_type)"
        " VALUES (%s, %s, 'depends_on');",
        (beta, alpha),
    )

    storage.drop_project(cursor, alpha)

    for table, column in storage.PROJECT_COLUMNS:
        cursor.execute(
            f"SELECT count(*) FROM {table} WHERE {column} = %s;",  # noqa: S608
            (alpha,),
        )
        assert cursor.fetchone() == (0,), f"{table}.{column}"
    cursor.execute("SELECT count(*) FROM chunks WHERE project = %s;", (beta,))
    assert cursor.fetchone() == (1,)


def test_a_chunk_for_a_node_that_is_gone_is_refused(cursor: Cursor) -> None:
    """What the key refused, the writer refuses: no chunk outlives its node."""
    project = f"alpha-{uuid.uuid4().hex[:8]}"
    storage.ensure_project(cursor, project, f"/code/{project}")
    with pytest.raises(storage.NodeGone):
        storage.replace_file_embeddings(
            cursor,
            project,
            "src/gone.py",
            "abc",
            "model-a",
            [(0, 1, 3, "body", [0.1] * EMBED_DIM)],
        )
