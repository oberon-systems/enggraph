"""Run the server's statements, and hand rows back as the previous driver did.

The SQL is kept as it was written, with `$1` placeholders. Values come back
the way node-postgres parsed them: a bigint and a numeric as text, since the
code around every statement was written against that.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg2
from psycopg2.extensions import connection as Connection
from psycopg2.pool import ThreadedConnectionPool

from enggraph.core.storage import get_db_url
from enggraph.mcp import jsjson

POOL_SIZE = 10
INT8 = 20
NUMERIC = 1700
INT8_ARRAY = 1016
NUMERIC_ARRAY = 1231
PLACEHOLDER = re.compile(r"\$(\d+)(?!\d)")
CAST = re.compile(r"\$(\d+)::([A-Za-z0-9_]+(?:\[\])?)")

Row = dict[str, Any]

_lock = threading.Lock()
_pool: ThreadedConnectionPool | None = None
# The pool refuses a caller when it is empty; this makes the caller wait.
_slots = threading.BoundedSemaphore(POOL_SIZE)


def pool() -> ThreadedConnectionPool:
    """Return the pool, opened on first use."""
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadedConnectionPool(1, POOL_SIZE, get_db_url())
        return _pool


def translate(sql: str) -> str:
    """Rewrite `$n` into the named placeholders psycopg2 takes.

    A server-side parameter has one type wherever it appears, set by its
    cast; a value written into the text has none, so the cast is repeated.
    """
    casts: dict[str, set[str]] = {}
    for index, cast in CAST.findall(sql):
        casts.setdefault(index, set()).add(cast.lower())

    def place(match: re.Match[str]) -> str:
        index = match.group(1)
        bare = not sql.startswith("::", match.end())
        known = casts.get(index, set())
        suffix = f"::{next(iter(known))}" if bare and len(known) == 1 else ""
        return f"%(p{index})s{suffix}"

    return PLACEHOLDER.sub(place, sql).replace("%", "%%").replace("%%(p", "%(p")


def named(params: Sequence[Any]) -> dict[str, Any]:
    """Name positional values the way `translate` names their placeholders."""
    return {
        f"p{index}": jsjson.dumps(value) if isinstance(value, dict) else value
        for index, value in enumerate(params, start=1)
    }


def _texts(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, list):
        return [_texts(item) for item in value]
    return None if value is None else str(value)


def _shaped(value: Any, type_code: int) -> Any:  # noqa: ANN401
    if value is None:
        return None
    if type_code in (INT8, NUMERIC):
        return str(value)
    if type_code in (INT8_ARRAY, NUMERIC_ARRAY):
        return _texts(value)
    return value


class Session:
    """One connection, for the statements that must share it."""

    def __init__(self, conn: Connection) -> None:
        """Take the connection the statements run on."""
        self._conn = conn

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        """Run one statement and return its rows, empty when it returns none."""
        return self.run(sql, params)[0]

    def run(self, sql: str, params: Sequence[Any] = ()) -> tuple[list[Row], int]:
        """Run one statement and return its rows and how many it touched."""
        with self._conn.cursor() as cursor:
            if params:
                cursor.execute(translate(sql), named(params))
            else:
                cursor.execute(sql)
            if cursor.description is None:
                return [], cursor.rowcount
            columns = [(column.name, column.type_code) for column in cursor.description]
            rows = [
                {
                    name: _shaped(value, type_code)
                    for (name, type_code), value in zip(columns, row, strict=True)
                }
                for row in cursor.fetchall()
            ]
            return rows, cursor.rowcount


@contextmanager
def session() -> Iterator[Session]:
    """Lend a connection that commits every statement on its own."""
    with _slots:
        conn = pool().getconn()
        broken = False
        try:
            conn.autocommit = True
            yield Session(conn)
        except psycopg2.InterfaceError:
            broken = True
            raise
        finally:
            pool().putconn(conn, close=broken or bool(conn.closed))


@contextmanager
def transaction() -> Iterator[Session]:
    """Lend a connection whose statements commit together, or not at all."""
    with _slots:
        conn = pool().getconn()
        try:
            conn.autocommit = False
            yield Session(conn)
            conn.commit()
        except BaseException:
            if not conn.closed:
                conn.rollback()
            raise
        finally:
            pool().putconn(conn, close=bool(conn.closed))


def query(sql: str, params: Sequence[Any] = ()) -> list[Row]:
    """Run one statement on a pooled connection and return its rows."""
    with session() as lent:
        return lent.query(sql, params)


def run(sql: str, params: Sequence[Any] = ()) -> tuple[list[Row], int]:
    """Run one statement and return its rows and how many it touched."""
    with session() as lent:
        return lent.run(sql, params)


def close() -> None:
    """Close every pooled connection, when the server stops."""
    global _pool
    with _lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
