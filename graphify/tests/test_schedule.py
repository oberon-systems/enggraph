"""When a project indexes itself, and which level decided that.

The decision is a pure one - three fields resolved over three levels, then
compared against a clock passed in - so all of it is pinned here without a
database, a mount or a wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.test_storage import FakeCursor

from enggraph.schedule import Schedule, due, next_due, resolve

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def cursor(**levels: dict) -> FakeCursor:
    """Build a cursor holding one indexing object per level, keyed as rows are."""
    objects = {}
    for key, value in levels.items():
        objects["_settings" if key == "global" else "alpha"] = {"indexing": value}
    return FakeCursor(objects=objects)


def schedule(mode: str, interval: int = 60, debounce: int = 5) -> Schedule:
    """Build one answer, for the clock comparison to work on."""
    return Schedule(mode, interval, debounce, {})


def test_a_project_nobody_configured_is_indexed_by_hand() -> None:
    """No level saying anything is the mode every project had before this."""
    settled = resolve(cursor(), "alpha")
    assert settled.mode == "off"
    assert settled.origins == {
        "mode": "default",
        "interval_minutes": "default",
        "debounce_minutes": "default",
    }


def test_the_global_default_reaches_a_project_that_says_nothing() -> None:
    """One row settles every project that has not overridden it."""
    settled = resolve(cursor(**{"global": {"mode": "periodic"}}), "alpha")
    assert settled.mode == "periodic"
    assert settled.origins["mode"] == "global"


def test_a_project_overrides_one_field_and_inherits_the_rest() -> None:
    """The fields are resolved apart, as the two selection documents are."""
    settled = resolve(
        cursor(
            **{
                "global": {"mode": "periodic", "interval_minutes": 30},
                "project": {"mode": "auto"},
            }
        ),
        "alpha",
    )
    assert (settled.mode, settled.interval_minutes) == ("auto", 30)
    assert settled.origins["mode"] == "project"
    assert settled.origins["interval_minutes"] == "global"


def test_a_watched_project_says_so(cursor_factory: None = None) -> None:
    """`watched` is the mode read back, which is what the scheduler acts on."""
    assert resolve(cursor(**{"project": {"mode": "auto"}}), "alpha").watched
    assert not resolve(cursor(**{"project": {"mode": "periodic"}}), "alpha").watched


def test_a_project_mounted_whole_reports_its_own_row_as_the_project() -> None:
    """One row is one level: the empty alias is not a directory of its own."""
    settled = resolve(cursor(**{"project": {"mode": "auto"}}), "alpha")
    assert settled.origins["mode"] == "project"


def test_a_mode_nobody_implements_is_ignored() -> None:
    """A value written by hand cannot make the scheduler act on nonsense."""
    settled = resolve(cursor(**{"project": {"mode": "whenever"}}), "alpha")
    assert settled.mode == "off"
    assert settled.origins["mode"] == "default"


def test_an_interval_of_zero_is_clamped_rather_than_obeyed() -> None:
    """Zero minutes would ask for a run on every tick, and get one."""
    settled = resolve(
        cursor(**{"project": {"mode": "periodic", "interval_minutes": 0}}),
        "alpha",
    )
    assert settled.interval_minutes == 1


def test_an_interval_that_is_not_a_number_says_nothing() -> None:
    """A string is not a duration, and the level above answers instead."""
    settled = resolve(
        cursor(
            **{
                "global": {"interval_minutes": 45},
                "project": {"interval_minutes": "soon"},
            }
        ),
        "alpha",
    )
    assert settled.interval_minutes == 45


def test_nothing_is_ever_owed_while_a_project_is_off() -> None:
    """Off is manual only, however long it has been and whatever changed."""
    settled = schedule("off")
    assert due(settled, None, True, NOW) is None
    assert due(settled, NOW - timedelta(days=30), True, NOW) is None


def test_a_project_never_indexed_is_owed_a_run_at_once() -> None:
    """There is no last run to wait an interval from, and no graph either."""
    settled = schedule("periodic")
    assert due(settled, None, False, NOW) == "periodic"


def test_a_timer_waits_out_its_interval() -> None:
    """The interval is a floor, not a target."""
    settled = schedule("periodic", interval=60)
    assert due(settled, NOW - timedelta(minutes=59), False, NOW) is None
    assert due(settled, NOW - timedelta(minutes=60), False, NOW) == "periodic"


def test_a_change_inside_the_debounce_window_waits() -> None:
    """The throttle is what keeps a busy checkout from indexing continuously."""
    settled = schedule("auto", interval=60, debounce=5)
    assert due(settled, NOW - timedelta(minutes=2), True, NOW) is None
    assert due(settled, NOW - timedelta(minutes=5), True, NOW) == "changed"


def test_a_watched_project_still_indexes_when_nothing_was_noticed() -> None:
    """The sweep behind auto: a blind watch must not mean a stale graph."""
    settled = schedule("auto", interval=60, debounce=5)
    assert due(settled, NOW - timedelta(minutes=30), False, NOW) is None
    assert due(settled, NOW - timedelta(minutes=60), False, NOW) == "fallback"


def test_the_next_sweep_is_an_interval_after_the_last_run() -> None:
    """What the dashboard shows when nothing has changed yet."""
    settled = schedule("periodic", interval=15)
    last = NOW - timedelta(minutes=5)
    assert next_due(settled, last) == last + timedelta(minutes=15)
    assert next_due(schedule("off"), last) is None
