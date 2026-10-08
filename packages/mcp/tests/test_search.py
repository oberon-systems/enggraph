"""How a query becomes the terms the lexical half searches by."""

from __future__ import annotations

from enggraph.mcp.search import lexical_terms, stem


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
