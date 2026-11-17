"""Finding the line an entity is summarized by."""

from __future__ import annotations

import pytest

from enggraph.indexer.summaries import entity_summary

LONG_SIGNATURE = '''def run(
    a: int,
    b: int,
    c: int,
    d: int,
    e: int,
    f: int,
) -> None:
    """Run it."""
'''

COMMENT_AFTER_COLON = '''def run(self) -> None:  # noqa: N802 - the base class names it
    """Run it."""
'''

LAMBDA_DEFAULT = '''def run(key=lambda x: x) -> None:
    """Run it."""
'''

BRACKET_IN_STRING = '''def run(sep: str = "(", mark: str = "#") -> None:
    """Run it."""
'''

LONG_BASES = '''class Run(
    Base,
    Mixin,
):
    """Run it."""
'''

OPEN_DOCSTRING = '''async def run(
    a: dict[str, int] = {"a": 1},
) -> None:

    """
    Run it.
    """
'''

RAW_DOCSTRING = '''def run() -> None:
    r"""Run it."""
'''


@pytest.mark.parametrize(
    "source",
    [
        LONG_SIGNATURE,
        COMMENT_AFTER_COLON,
        LAMBDA_DEFAULT,
        BRACKET_IN_STRING,
        LONG_BASES,
        OPEN_DOCSTRING,
        RAW_DOCSTRING,
    ],
    ids=[
        "long-signature",
        "comment-after-colon",
        "lambda-default",
        "bracket-in-string",
        "long-bases",
        "open-docstring",
        "raw-docstring",
    ],
)
def test_a_docstring_is_found_past_whatever_the_signature_holds(source: str) -> None:
    """The signature ends at its closing colon, however many lines it takes."""
    assert entity_summary(source.splitlines(), 1) == "Run it."


def test_without_a_docstring_the_comment_above_is_taken() -> None:
    """A long signature with no docstring still falls back to the comment."""
    lines = ["# Run it.", "def run(", "    a: int,", ") -> int:", "    return a"]
    assert entity_summary(lines, 2) == "Run it."


def test_a_one_line_body_does_not_take_the_next_entity_s_docstring() -> None:
    """A signature that ends without a colon has no docstring below it."""
    lines = ["def run(): return 1", "", "def walk():", '    """Walk it."""']
    assert entity_summary(lines, 1) == ""
