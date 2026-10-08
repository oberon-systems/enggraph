"""A level's schedule and features are read, and its tokens stay here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enggraph.core import jsjson
from enggraph.web.args import HttpError
from enggraph.web.features import read_feature, redact_keys, require_feature
from enggraph.web.indexing import read_indexing


def test_a_body_that_states_nothing_is_no_schedule() -> None:
    """Hold that a body that states nothing is no schedule."""
    assert read_indexing({}) is None
    assert read_indexing({"mode": "", "interval_minutes": None}) is None


def test_a_schedule_keeps_what_was_stated() -> None:
    """Hold that a schedule keeps what was stated."""
    body = {"mode": "auto", "interval_minutes": 30, "enabled": False, "allowed": True}
    assert read_indexing(body) == {
        "mode": "auto",
        "interval_minutes": 30,
        "enabled": False,
    }
    assert read_indexing(body, root=True)["allowed"] is True


@pytest.mark.parametrize(
    "body",
    [{"mode": "hourly"}, {"interval_minutes": 0}, {"enabled": "yes"}, "text"],
)
def test_a_wrong_schedule_is_refused(body: object) -> None:
    """Hold that a wrong schedule is refused."""
    with pytest.raises(HttpError):
        read_indexing(body)


def test_a_feature_tells_null_from_absent() -> None:
    """Hold that a feature tells null from absent."""
    assert read_feature({}) is None
    assert read_feature({"enabled": None, "batch": None}) == {
        "enabled": None,
        "batch": None,
    }
    assert read_feature({"server_url": "http://x/ "}) == {"server_url": "http://x"}
    assert read_feature({"server_url": ""}) == {"server_url": None}


def test_only_the_global_level_says_what_the_loop_reads() -> None:
    """Hold that only the global level says what the loop reads."""
    body = {"allowed": False, "tick_seconds": 9, "batch": 2}
    assert read_feature(body) == {"batch": 2}
    assert read_feature(body, root=True) == {
        "allowed": False,
        "batch": 2,
        "tick_seconds": 9,
    }


def test_a_token_is_stored_with_the_moment_it_was() -> None:
    """Hold that a token is stored with the moment it was."""
    read = read_feature({"server_key": " secret "})
    assert read is not None
    assert read["server_key"] == "secret"
    assert read["key_saved_at"].endswith("Z")
    assert read_feature({"server_key": "  "}) is None


@pytest.mark.parametrize(
    "body",
    [{"server_url": "ftp://x"}, {"enabled": 1}, {"batch": 65}, None],
)
def test_a_wrong_feature_is_refused(body: object) -> None:
    """Hold that a wrong feature is refused."""
    with pytest.raises(HttpError):
        read_feature(body)


def test_an_unknown_feature_is_refused() -> None:
    """Hold that an unknown feature is refused."""
    assert require_feature("embedding") == "embedding"
    with pytest.raises(HttpError):
        require_feature("gamma")


def test_a_token_never_reaches_a_browser() -> None:
    """Hold that a token never reaches a browser."""
    old = jsjson.instant(datetime.now(UTC) - timedelta(days=40))
    out = redact_keys(
        {
            "embedding": {"server_key": "secret", "key_saved_at": old, "batch": 2},
            "summarize": {"server_key": "secret"},
            "indexing": {"mode": "off"},
            "plain": 5,
        }
    )
    assert out is not None
    assert "secret" not in jsjson.dumps(out)
    assert out["embedding"]["key_set"] is True
    assert out["embedding"]["key_expired"] is True
    assert out["summarize"]["key_due"] is None
    assert out["summarize"]["key_expired"] is False
    assert out["indexing"] == {"mode": "off"}
    assert out["plain"] == 5
    assert redact_keys(None) is None
