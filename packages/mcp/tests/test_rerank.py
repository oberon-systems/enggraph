"""What lifts a fused search row and what sinks it."""

from __future__ import annotations

from typing import Any

from enggraph.mcp.rerank import rerank


def row(node_id: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    """One fused row, as the search statement returns it."""
    return {
        "project": "alpha",
        "project_type": "codebase",
        "id": node_id,
        "name": node_id,
        "type": "function",
        "file_path": None,
        "rrf": 0.01,
        "lexical_rank": 1,
        "vector_rank": None,
        "in_degree": 0,
        **fields,
    }


def ids(ranked: list[dict[str, Any]]) -> list[str]:
    """Return the ids of ranked rows, in order."""
    return [item["row"]["id"] for item in ranked]


def test_an_exact_identifier_beats_a_better_fused_score() -> None:
    """A query naming a symbol wants that symbol."""
    rows = [row("other", rrf=0.015), row("hit", name="readLimit", rrf=0.01)]
    assert ids(rerank(rows, "where is readLimit set", 10, True)) == ["hit", "other"]


def test_an_identifier_shaped_token_outweighs_a_plain_word() -> None:
    """`queue` may be a word; `readLimit` is a name."""
    rows = [row("plain", name="queue"), row("shaped", name="readLimit")]
    assert ids(rerank(rows, "queue readLimit", 10, True)) == ["shaped", "plain"]


def test_query_words_found_in_the_path_are_rewarded() -> None:
    """A path is the cheapest description a file has."""
    rows = [
        row("elsewhere", file_path="src/other/thing.ts"),
        row("matching", file_path="src/payment/refund.ts"),
    ]
    assert ids(rerank(rows, "payment refund", 10, True))[0] == "matching"


def test_external_placeholders_and_prose_sink_below_code() -> None:
    """A heading in a codebase is not the code it describes."""
    rows = [
        row("ext", type="external_module"),
        row("heading", type="heading"),
        row("code"),
    ]
    ranked = rerank(rows, "nothing relevant", 10, True)
    assert ids(ranked) == ["code", "heading", "ext"]


def test_prose_in_a_docs_project_is_not_penalised() -> None:
    """There the prose is the content."""
    rows = [row("code", rrf=0.0101), row("doc", type="heading", project_type="docs")]
    ranked = rerank(rows, "nothing relevant", 10, True)
    assert ids(ranked)[0] == "code"
    assert ranked[0]["score"] - ranked[1]["score"] < 0.001


def test_rows_sharing_a_file_with_other_candidates_are_rewarded() -> None:
    """Two hits in one file say more than one hit in each of two."""
    rows = [
        row("alone", file_path="src/a.ts"),
        row("pair1", file_path="src/b.ts"),
        row("pair2", file_path="src/b.ts"),
    ]
    assert ids(rerank(rows, "nothing relevant", 10, True))[2] == "alone"


def test_in_degree_is_rewarded() -> None:
    """What much of the code points at is likelier the answer."""
    rows = [row("leaf"), row("hub", in_degree=20)]
    assert ids(rerank(rows, "nothing relevant", 10, True))[0] == "hub"


def test_vendored_paths_sink_below_own_code() -> None:
    """Somebody else's copy of a name is rarely what was asked about."""
    rows = [
        row("dep", file_path="lib/vendor/alpha/store.php", rrf=0.016),
        row("own", file_path="src/store.php", rrf=0.01),
    ]
    assert ids(rerank(rows, "store", 10, True)) == ["own", "dep"]


def test_tests_sink_unless_the_question_asks_for_them() -> None:
    """A test mentions everything the code does."""
    rows = [
        row("spec", file_path="pkg/tests/test_hash.py", rrf=0.0165),
        row("impl", file_path="pkg/src/hash.py", rrf=0.0163),
    ]
    assert ids(rerank(rows, "compute the hash", 10, True)) == ["impl", "spec"]
    assert ids(rerank(rows, "tests for the hash", 10, True)) == ["spec", "impl"]


def test_disabled_it_returns_the_fused_order_and_score() -> None:
    """The switch is for telling what the reranker changed."""
    rows = [row("low", name="readLimit", rrf=0.01), row("high", rrf=0.02)]
    ranked = rerank(rows, "readLimit", 10, False)
    assert ids(ranked) == ["high", "low"]
    assert [item["score"] for item in ranked] == [0.02, 0.01]


def test_projects_are_interleaved_by_their_own_rank() -> None:
    """Each project's best row comes before any project's second."""
    rows = [
        row("a1", rrf=0.03),
        row("a2", rrf=0.02),
        row("b1", project="beta", rrf=0.01),
    ]
    assert ids(rerank(rows, "x", 10, False)) == ["a1", "b1", "a2"]


def test_ties_break_by_id_and_the_list_is_cut_to_the_limit() -> None:
    """The same question gives the same list."""
    rows = [row("c"), row("a"), row("b")]
    assert ids(rerank(rows, "x", 2, True)) == ["a", "b"]
