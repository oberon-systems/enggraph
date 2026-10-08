"""Placing neighbours into tiers and filling a packet within its budget."""

from __future__ import annotations

from typing import Any

import pytest

from enggraph.mcp.context import (
    SUMMARY_TIER_ORDER,
    assemble,
    cap_for,
    classify,
    estimate_tokens,
    is_importer,
    is_test_path,
    line_from_id,
    overlaps,
    resolve_detail,
    shorten,
)


def entry(node_id: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    """One entry of a packet."""
    return {
        "project": "alpha",
        "project_type": "codebase",
        "id": node_id,
        "name": node_id,
        "type": "function",
        "file_path": None,
        "start_line": None,
        "end_line": None,
        "origin": "expansion",
        "why": "test",
        "summary": None,
        **fields,
    }


def candidate(
    node_id: str,
    tier: str | None,
    chunk: str | None = None,
    seed_order: int = 0,
    place: int = 0,
    **entry_fields: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """One candidate for a packet, before the budget decides."""
    return {
        "key": ("alpha", node_id),
        "tier": tier,
        "chunk": chunk,
        "seedOrder": seed_order,
        "place": place,
        "entry": entry(node_id, **entry_fields),
    }


def ids(packed: dict[str, Any]) -> list[str]:
    """Return the ids of the entries a packet kept, in order."""
    return [one["id"] for one in packed["entries"]]


def test_four_characters_a_token_rounded_up() -> None:
    """An estimate, and the same one every time."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_auth.py", True),
        ("pkg/test/helper.ts", True),
        ("src/auth.test.ts", True),
        ("src/auth.spec.ts", True),
        ("pkg/test_refund.py", True),
        ("cmd/refund_test.go", True),
        ("src/latest.ts", False),
        ("src/contest/entry.ts", False),
        (None, False),
    ],
)
def test_a_test_is_known_by_its_path(path: str | None, expected: bool) -> None:
    """A directory, a prefix or a suffix, in any of the usual spellings."""
    assert is_test_path(path) is expected


def test_containment_is_placed_by_direction() -> None:
    """What holds a hit, and what a hit holds."""
    assert classify("contains", "incoming", "tests/a.py") == "container"
    assert classify("contains", "outgoing", "src/a.py") == "defines"


def test_a_test_file_is_in_the_test_tier_whatever_the_edge() -> None:
    """Where it lives says more than how it was reached."""
    assert classify("calls", "incoming", "tests/a.py") == "test"
    assert classify("imports", "outgoing", "src/a.test.ts") == "test"


def test_dependency_edges_are_read_by_direction() -> None:
    """Being imported is this graph's nearest thing to being called."""
    assert classify("imports", "incoming", "src/a.ts") == "caller"
    assert classify("imports", "outgoing", "src/a.ts") == "import"
    assert classify("calls", "incoming", "src/a.ts") == "caller"
    assert classify("calls", "outgoing", "src/a.ts") == "callee"


def test_the_bulk_tiers_decay_with_seed_rank() -> None:
    """A map of the eighth hit is a table of contents nobody asked for."""
    assert cap_for("defines", 0) == 8
    assert cap_for("defines", 3) == 2
    assert cap_for("defines", 5) == 0
    assert cap_for("import", 2) == 1
    assert cap_for("import", 3) == 0


def test_importers_decay_apart_from_the_callers_they_sit_beside() -> None:
    """They share the tier and not its budget."""
    assert is_importer("imports_from", "incoming") is True
    assert is_importer("imports_from", "outgoing") is False
    assert is_importer("calls", "incoming") is False
    assert cap_for("caller", 0, True) == 3
    assert cap_for("caller", 2, True) == 1
    assert cap_for("caller", 3, True) == 0
    assert cap_for("caller", 3) == 8


def test_the_scarce_tiers_stay_flat() -> None:
    """What depends on a hit is scarce enough at any rank."""
    assert cap_for("caller", 7) == 8
    assert cap_for("test", 7) == 4


def test_the_line_a_symbol_id_carries_is_read() -> None:
    """Nothing else records it."""
    assert line_from_id("src/a.py::refund@L70") == 70
    assert line_from_id("src/a.py") is None


def test_a_path_is_shortened_to_its_basename_and_keeps_the_symbol() -> None:
    """The entry beside it already carries the whole path."""
    assert shorten("src/pay/refund.py::Refund@L3") == "refund.py::Refund@L3"
    assert shorten("src/pay/refund.py") == "refund.py"
    assert shorten("top.py") == "top.py"


KEPT = [entry("a", file_path="src/a.ts", start_line=10, end_line=20)]


def test_overlapping_line_ranges_in_one_file_are_found() -> None:
    """Sharing one line is overlapping."""
    other = entry("b", file_path="src/a.ts", start_line=20, end_line=30)
    assert overlaps(KEPT, other) is True


def test_disjoint_ranges_other_files_and_other_projects_do_not_overlap() -> None:
    """The same lines of another file are other lines."""
    apart = entry("b", file_path="src/a.ts", start_line=21, end_line=30)
    elsewhere = entry("b", file_path="src/b.ts", start_line=10, end_line=20)
    foreign = entry(
        "b", project="beta", file_path="src/a.ts", start_line=10, end_line=20
    )
    assert overlaps(KEPT, apart) is False
    assert overlaps(KEPT, elsewhere) is False
    assert overlaps(KEPT, foreign) is False


def test_an_entry_without_lines_is_never_dropped() -> None:
    """There is nothing to compare."""
    assert overlaps(KEPT, entry("b", file_path="src/a.ts")) is False


def test_seeds_come_first_then_the_tiers_in_their_fixed_order() -> None:
    """What depends on a hit before what it depends on."""
    expanded = [
        candidate("imp", "import"),
        candidate("def", "defines"),
        candidate("call", "caller"),
        candidate("tst", "test"),
    ]
    packed = assemble([candidate("seed", None)], expanded, 10000, False)
    assert ids(packed) == ["seed", "call", "tst", "def", "imp"]
    assert packed["truncated"] is False


def test_seeds_are_interleaved_within_a_tier_by_place() -> None:
    """Every seed's first neighbour before any seed's second."""
    expanded = [
        candidate("s0p1", "caller", seed_order=0, place=1),
        candidate("s1p0", "caller", seed_order=1, place=0),
        candidate("s0p0", "caller", seed_order=0, place=0),
    ]
    assert ids(assemble([], expanded, 10000, False)) == ["s0p0", "s1p0", "s0p1"]


def test_an_expanded_entry_inside_a_kept_range_is_dropped() -> None:
    """Its lines are in the packet already."""
    seeds = [candidate("seed", None, file_path="src/a.ts", start_line=1, end_line=50)]
    expanded = [
        candidate("inner", "defines", file_path="src/a.ts", start_line=5, end_line=9)
    ]
    assert ids(assemble(seeds, expanded, 10000, False)) == ["seed"]


def test_a_seed_keeps_its_bare_reference_when_its_chunk_does_not_fit() -> None:
    """The reference alone is worth keeping when the text is not."""
    packed = assemble([candidate("seed", None, chunk="x" * 4000)], [], 200, True)
    assert len(packed["entries"]) == 1
    assert "chunk" not in packed["entries"][0]
    assert packed["truncated"] is True
    assert packed["used"] <= 200


def test_expanded_entries_get_chunks_only_after_everything_is_placed() -> None:
    """A chunk is an upgrade, never a reason to leave an entry out."""
    expanded = [candidate("a", "caller", chunk="a" * 40), candidate("b", "import")]
    packed = assemble([], expanded, 10000, True)
    assert ids(packed) == ["a", "b"]
    assert packed["entries"][0]["chunk"] == "a" * 40


def test_the_budget_is_never_overspent() -> None:
    """Whatever does not fit is left out and said so."""
    expanded = [candidate(f"n{at:02d}", "defines", place=at) for at in range(50)]
    packed = assemble([], expanded, 300, False)
    assert packed["used"] <= 300
    assert packed["truncated"] is True
    assert 0 < len(packed["entries"]) < 50


def test_an_explicit_detail_is_kept() -> None:
    """`auto` is the only value that is settled here."""
    assert resolve_detail("source", "how is auth organised", 12000) == "source"
    assert resolve_detail("summary", "readLimit", 12000) == "summary"


@pytest.mark.parametrize(
    "query",
    [
        "who calls readLimit",
        "what does queue_embeddings do",
        "explain src/alpha.ts",
        "when is claim() retried",
    ],
)
def test_a_question_naming_code_gets_source(query: str) -> None:
    """An identifier, a path or a call is a name."""
    assert resolve_detail("auto", query, 12000) == "source"


def test_a_question_in_words_or_a_small_budget_gets_the_summary_ladder() -> None:
    """Source cannot fit beside a map in a small budget."""
    words = "how is the payment flow organised"
    assert resolve_detail("auto", words, 12000) == "summary"
    assert resolve_detail("auto", "who calls readLimit", 2000) == "summary"


def test_summary_order_spends_the_ladder_first_and_keeps_expansions_bare() -> None:
    """Where a hit sits, then what sits beside it, then what calls it."""
    expanded = [
        candidate("call", "caller", chunk="c" * 40),
        candidate("dir", "ancestor"),
        candidate("def", "defines"),
    ]
    packed = assemble(
        [candidate("seed", None)], expanded, 10000, True, SUMMARY_TIER_ORDER, False
    )
    assert ids(packed) == ["seed", "dir", "def", "call"]
    assert "chunk" not in packed["entries"][3]
