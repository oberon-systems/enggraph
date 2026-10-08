"""Requests are read the way the previous server read them."""

from __future__ import annotations

import math

import pytest

from enggraph.web import args


def test_a_number_is_read_as_javascript_reads_it() -> None:
    """Hold that a number is read as javascript reads it."""
    assert args.number("10") == 10
    assert args.number(" 1e1 ") == 10
    assert math.isnan(args.number("ten"))
    assert args.whole(5.0)
    assert not args.whole(1.5)
    assert not args.whole(math.nan)


def test_a_missing_field_is_not_a_null_one() -> None:
    """Hold that a missing field is not a null one."""
    assert args.field({"a": None}, "a") is None
    assert args.field({}, "a") is args.MISSING
    assert args.field([], "a") is args.MISSING


def test_a_string_field_is_held_to_being_one() -> None:
    """Hold that a string field is held to being one."""
    assert args.read_body_string({"a": "x"}, "a") == "x"
    assert args.read_body_string({}, "a") is None
    with pytest.raises(args.HttpError) as refused:
        args.read_body_string({"a": None}, "a")
    assert refused.value.status == 400
    with pytest.raises(args.HttpError):
        args.read_body_string("text", "a")


def test_a_number_field_is_whole_and_within_bounds() -> None:
    """Hold that a number field is whole and within bounds."""
    assert args.read_body_number({"a": 5.0}, "a", 1, 10) == 5
    assert args.read_body_number({"a": None}, "a", 1, 10) is None
    for wrong in (1.5, True, "5", 0, 11):
        with pytest.raises(args.HttpError):
            args.read_body_number({"a": wrong}, "a", 1, 10)


def test_required_and_enum_fields() -> None:
    """Hold that required and enum fields."""
    assert args.read_body_enum({"a": ""}, "a", ("x",)) is None
    assert args.read_body_enum({"a": "x"}, "a", ("x",)) == "x"
    with pytest.raises(args.HttpError):
        args.read_body_enum({"a": "y"}, "a", ("x",))
    with pytest.raises(args.HttpError):
        args.require_body_string({"a": "  "}, "a")


def test_text_spells_values_as_string_does() -> None:
    """Hold that text spells values as string does."""
    assert args.text(5) == "5"
    assert args.text(True) == "true"
    assert args.text(None) == "null"
    assert args.text(["a", None, 1]) == "a,,1"


def test_a_reply_is_json_as_the_previous_server_spelled_it() -> None:
    """Hold that a reply is json as the previous server spelled it."""
    answer = args.reply({"a": 1.0, "b": None}, 201)
    assert answer.status_code == 201
    assert answer.body == b'{"a":1,"b":null}'
    assert answer.headers["content-type"] == "application/json; charset=utf-8"
