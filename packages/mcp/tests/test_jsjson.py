"""Numbers are spelled and rounded the way the previous server did."""

from __future__ import annotations

from enggraph.mcp import jsjson


def test_a_tie_rounds_up_as_to_fixed_does() -> None:
    """1/64 is exact in binary, and format() would give the even 0.01562."""
    assert jsjson.fixed(0.015625, 5) == 0.01563
    assert jsjson.fixed(1 / 61, 5) == 0.01639
    assert jsjson.fixed(0.0, 5) == 0.0


def test_a_whole_float_is_written_without_a_fraction() -> None:
    """JSON.stringify writes 1, never 1.0."""
    assert jsjson.dumps({"a": 1.0, "b": 0.5}, 0) == '{"a":1,"b":0.5}'
