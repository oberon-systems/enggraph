"""Cutting a file into the pieces a vector is written for.

Pure text in, pieces out: no database, no mount, no model, so every rule the
chunker has is pinned here.
"""

from __future__ import annotations

from enggraph.chunks import split


def numbered(count: int) -> str:
    """Return a file of `count` lines, each naming its own number."""
    return "\n".join(f"line {index}" for index in range(1, count + 1))


def test_a_short_file_is_one_chunk_covering_every_line() -> None:
    """Nothing is split that fits, and the range covers the whole file."""
    chunks = split(numbered(5), max_chars=1000, overlap_lines=2)
    assert len(chunks) == 1
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 5)
    assert chunks[0].text == numbered(5)


def test_line_numbers_are_one_based_and_inclusive() -> None:
    """The way an editor counts, because that is what the range is read as."""
    chunks = split(numbered(40), max_chars=40, overlap_lines=0)
    assert chunks[0].start_line == 1
    assert chunks[1].start_line == chunks[0].end_line + 1


def test_windows_overlap_by_the_lines_asked_for() -> None:
    """A declaration on a boundary is whole in one of the two chunks."""
    chunks = split(numbered(40), max_chars=40, overlap_lines=2)
    assert len(chunks) > 1
    assert chunks[1].start_line == chunks[0].end_line - 1


def test_an_empty_file_is_no_chunks_at_all() -> None:
    """A vector of the model's opinion of nothing would match every nothing."""
    assert split("") == []
    assert split("   \n\n\t\n") == []


def test_a_line_longer_than_the_window_is_not_cut_in_half() -> None:
    """A minified bundle is one line, and half a token embeds nothing."""
    chunks = split("x" * 5000, max_chars=100, overlap_lines=1)
    assert len(chunks) == 1
    assert len(chunks[0].text) == 5000


def test_every_line_of_a_file_lands_in_some_chunk() -> None:
    """The windows cover the file: nothing between two of them is dropped."""
    text = numbered(200)
    covered: set[int] = set()
    for chunk in split(text, max_chars=80, overlap_lines=3):
        covered.update(range(chunk.start_line, chunk.end_line + 1))
    assert covered == set(range(1, 201))


def test_the_chunks_are_numbered_in_file_order() -> None:
    """The index is what the unique key stores, so it counts from zero up."""
    chunks = split(numbered(100), max_chars=60, overlap_lines=1)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_a_window_smaller_than_the_overlap_still_terminates() -> None:
    """The step back never reaches the start of the window it stepped from."""
    chunks = split(numbered(20), max_chars=1, overlap_lines=10)
    assert len(chunks) == 20
