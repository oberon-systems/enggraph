"""What the second pass refuses to write, and what invalidates the first.

Neither needs a database: `link_file` decides which edges to attempt before
it sends anything, and the hash and revision it decides on are pure functions.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from enggraph.indexer import indexer
from enggraph.indexer.indexer import (
    compute_hash,
    legacy_hash,
    link_file,
    stored_current,
)
from enggraph.indexer.parsers import registry, revision_for
from enggraph.indexer.parsers.languages import JsonParser

PATH = "docker-compose.yaml"
COMPOSE = """
services:
  web:
    image: nginx:1.27
"""
# What the old YAML parser left behind for the same file, before the compose
# parser took it over: a node named after the top level key.
STALE = [{"id": f"{PATH}::key.services", "name": "key.services", "type": "key"}]
CURRENT = [{"name": "service.web", "type": "service"}]


@pytest.fixture
def edges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Collect the edges `link_file` sends, instead of sending them."""
    written: list[tuple[str, str, str]] = []

    def record(
        cursor: Any,  # noqa: ANN401
        project: str,
        source_id: str,
        target_id: str,
        relation_type: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        written.append((source_id, target_id, relation_type))

    monkeypatch.setattr(indexer, "insert_edge", record)
    monkeypatch.setattr(indexer, "ensure_external_node", lambda *args: None)
    return written


def run(entities: list[dict[str, str]]) -> int:
    """Link the compose file against the entity set it is handed."""
    return link_file(None, "demo", PATH, COMPOSE, set(), {}, entities)


def test_an_edge_from_an_undeclared_entity_is_dropped(
    edges: list[tuple[str, str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stale nodes must not make the run attempt an edge nothing can hold.

    The database would refuse the foreign key, and the refusal aborts the
    transaction - which costs the file every edge, not the one.
    """
    with caplog.at_level("WARNING"):
        count = run(STALE)
    assert count == 1
    assert edges == [(PATH, f"{PATH}::key.services", "contains")]
    assert "service.web" in caplog.text


def test_the_same_edge_is_written_once_the_entity_is_there(
    edges: list[tuple[str, str, str]],
) -> None:
    """With the node the current parser declares, the edge is written."""
    run(CURRENT)
    assert (f"{PATH}::service.web", "image:nginx:1.27", "uses_image") in edges


def test_the_hash_is_the_content_alone() -> None:
    """The parsers no longer move it: their revision is stored beside it."""
    assert compute_hash(COMPOSE) == hashlib.md5(COMPOSE.encode()).hexdigest()
    assert legacy_hash(COMPOSE) != compute_hash(COMPOSE)


def test_a_parser_revision_covers_what_that_parser_runs() -> None:
    """The Scala parser changing must not reparse a JSON file."""
    out: list[str] = []
    registry._fold(JsonParser.__module__, JsonParser.__name__, set(), out)
    folded = {one.split("\0", 1)[0].rsplit(".", 1)[1] for one in out}
    assert {"JsonParser", "CodeParser", "pair_key"} <= folded
    assert "ScalaParser" not in folded
    assert revision_for("a.json") != revision_for("b.scala")
    assert revision_for("a.unknown") == "none"


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stand in for the stored hash and record what is adopted."""
    state: dict[str, Any] = {"row": None, "adopted": []}
    monkeypatch.setattr(
        indexer, "get_file_hash", lambda cursor, project, path: state["row"]
    )
    monkeypatch.setattr(
        indexer,
        "adopt_legacy_hash",
        lambda *args: state["adopted"].append(args[2:]),
    )
    return state


def test_a_file_never_stored_is_read(stored: dict[str, Any]) -> None:
    """Nothing stored is nothing current."""
    assert stored_current(None, "alpha", PATH, COMPOSE, "r1") == (
        False,
        compute_hash(COMPOSE),
    )


def test_a_file_is_current_only_under_the_same_revision(
    stored: dict[str, Any],
) -> None:
    """Its own parser changing reads it again; nothing else does."""
    stored["row"] = (compute_hash(COMPOSE), "r1")
    assert stored_current(None, "alpha", PATH, COMPOSE, "r1")[0] is True
    assert stored_current(None, "alpha", PATH, COMPOSE, "r2")[0] is False
    assert stored_current(None, "alpha", PATH, COMPOSE + "x", "r1")[0] is False


def test_a_hash_stored_the_old_way_is_adopted_not_read_again(
    stored: dict[str, Any],
) -> None:
    """Moving to per-parser revisions must not reparse every tree once more."""
    stored["row"] = (legacy_hash(COMPOSE), None)
    assert stored_current(None, "alpha", PATH, COMPOSE, "r1")[0] is True
    assert stored["adopted"] == [
        (PATH, legacy_hash(COMPOSE), compute_hash(COMPOSE), "r1")
    ]
    stored["row"] = ("0" * 32, None)
    assert stored_current(None, "alpha", PATH, COMPOSE, "r1")[0] is False
