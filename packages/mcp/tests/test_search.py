"""How a query becomes the terms the lexical half searches by."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from enggraph.mcp import search
from enggraph.mcp.search import hybrid_search, lexical_terms, mode_note, stem

VECTOR = [0.25, 0.5]


class Recorder:
    """A database that keeps what it was asked and answers with no rows."""

    def __init__(self) -> None:
        """Start with nothing asked."""
        self.asked: list[tuple[str, list[Any]]] = []

    def query(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        """Keep the statement and its parameters."""
        self.asked.append((sql, list(params or [])))
        return []

    def hybrid(self) -> list[Any]:
        """Return the parameters the search statement was run with."""
        return next(params for sql, params in self.asked if sql == search.HYBRID)


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Stand in for the database and for the mounted trees."""
    recorder = Recorder()

    @contextmanager
    def transaction() -> Iterator[Recorder]:
        yield recorder

    monkeypatch.setattr(search.db, "transaction", transaction)
    monkeypatch.setattr(search, "read_ranges", lambda wanted: [])
    return recorder


@pytest.fixture
def embedded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for an embedding server, and return the queries it was sent."""
    sent: list[str] = []

    def embed(query: str, project: str | None) -> list[float]:
        sent.append(query)
        return VECTOR

    monkeypatch.setattr(search, "embed_query", embed)
    return sent


def test_a_plural_or_a_tense_is_cut_to_a_searchable_root() -> None:
    """The index does not stem, so the query is cut and searched as a prefix."""
    assert stem("refunds") == "refund"
    assert stem("changed") == "chang"
    assert stem("queries") == "quer"
    assert stem("stopped") == "stop"


def test_a_root_shorter_than_four_letters_is_not_cut_to() -> None:
    """A three-letter prefix matches half the dictionary."""
    assert stem("uses") == "uses"
    assert stem("files") == "file"


def test_a_consonant_is_undoubled_only_after_a_verb_ending() -> None:
    """`calls` is a plural, not `stopped`."""
    assert stem("calls") == "call"


def test_the_content_words_of_a_question_are_ored_as_prefixes() -> None:
    """Stopwords go, short roots stay whole."""
    terms = lexical_terms("how does the indexer skip a file that has not changed")
    assert terms["terms"] == ["indexer:*", "skip:*", "file:*", "has", "not", "chang:*"]


def test_camel_and_snake_case_split_the_way_the_index_splits_them() -> None:
    """Or the query and the index disagree about what a word is."""
    assert lexical_terms("where is PendingRefund handled")["terms"] == [
        "pend:*",
        "refund:*",
        "handl:*",
    ]
    assert lexical_terms("queue_depth of HTTPServer")["terms"] == [
        "queue:*",
        "depth:*",
        "http:*",
        "server:*",
    ]


def test_websearch_syntax_is_kept_when_the_caller_wrote_operators() -> None:
    """Quotes, OR and a leading minus are honoured as written."""
    assert lexical_terms('"file hash" -test')["terms"] is None
    assert lexical_terms("queue OR lease")["terms"] is None


def test_nothing_but_stopwords_falls_back_to_websearch() -> None:
    """An empty OR would match nothing at all."""
    assert lexical_terms("how is the")["terms"] is None


def test_identifier_shaped_tokens_become_name_patterns() -> None:
    """They name a symbol, and a symbol is found by its name."""
    names = lexical_terms("where is readLimit and queue_depth set")["names"]
    assert names == ["%readlimit%", "%queue_depth%"]


def test_the_default_mode_runs_both_halves(
    asked: Recorder, embedded: list[str]
) -> None:
    """What was passed before, and the switch that keeps the lexical half."""
    found = hybrid_search("alpha", None, "queue depth", 20)
    terms = lexical_terms("queue depth")
    assert embedded == ["queue depth"]
    assert asked.hybrid() == [
        "alpha",
        None,
        "%queue depth%",
        "queue depth",
        search.vector_literal(VECTOR),
        60,
        240,
        terms["terms"],
        terms["names"],
        search.DF_CAP,
        False,
        terms["words"],
        True,
    ]
    assert found["vectorAvailable"]


def test_lexical_mode_makes_no_embedding_call(
    asked: Recorder, embedded: list[str]
) -> None:
    """The vector goes in as NULL, so its half matches nothing."""
    found = hybrid_search("alpha", None, "queue depth", 20, mode="lexical")
    params = asked.hybrid()
    assert embedded == []
    assert params[4] is None
    assert params[12] is True
    assert not found["vectorAvailable"]


def test_vector_mode_switches_the_lexical_half_off(
    asked: Recorder, embedded: list[str]
) -> None:
    """No terms, no name patterns, and the switch every lexical list reads."""
    hybrid_search("alpha", None, "where is queue_depth set", 20, mode="vector")
    params = asked.hybrid()
    assert params[4] == search.vector_literal(VECTOR)
    assert (params[7], params[8], params[11]) == (None, [], [])
    assert params[12] is False


def test_vector_mode_without_a_vector_runs_no_statement(
    asked: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a silent answer from the other half."""
    monkeypatch.setattr(search, "embed_query", lambda query, project: None)
    found = hybrid_search("alpha", None, "queue depth", 20, mode="vector")
    assert asked.asked == []
    assert found == {"rows": [], "vectorAvailable": False, "embedded": False}


def test_every_lexical_list_reads_the_switch() -> None:
    """Three lists gather by words or names; the vector list does not."""
    assert search.HYBRID.count("$13::boolean") == 3
    vector_half = search.HYBRID.split("vector_hits AS (")[1].split("fused AS (")[0]
    assert "$13" not in vector_half


def test_the_note_names_the_mode_that_was_asked_for() -> None:
    """A missing half that was asked for is not a missing embedder."""
    nothing = {"rows": [], "vectorAvailable": False, "embedded": False}
    both = {"rows": [{"vector_rank": 1}], "vectorAvailable": True, "embedded": True}
    assert mode_note("lexical", nothing).startswith("Mode lexical: ")
    assert "unavailable" not in mode_note("lexical", nothing)
    assert mode_note("vector", both).startswith("Mode vector: ")
    assert mode_note("hybrid", both) == "Mode hybrid."
    assert mode_note("hybrid", nothing).startswith("Mode hybrid. Semantic half")
