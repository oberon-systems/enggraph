"""When a project is indexed without being asked, and where that was decided.

The schedule lives beside the selection, in `project_settings.settings`, and is
resolved the same way: the project, then the organizations holding it, then the
global default, most specific first and one field at a time. A project that
says nothing is indexed by hand, exactly as every project was before this
existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg2.extensions import cursor as Cursor

from enggraph.config import (
    DEFAULT_INDEX_DEBOUNCE_MINUTES,
    DEFAULT_INDEX_INTERVAL_MINUTES,
    DEFAULT_INDEXING_MODE,
    INDEXING_KEY,
    INDEXING_MODES,
    MAX_INDEX_DEBOUNCE_MINUTES,
    MAX_INDEX_INTERVAL_MINUTES,
    MIN_INDEX_DEBOUNCE_MINUTES,
    MIN_INDEX_INTERVAL_MINUTES,
)
from enggraph.selection import Origin, levels
from enggraph.storage import read_settings_json

LOG = logging.getLogger(__name__)

INTERVAL = "interval_minutes"
DEBOUNCE = "debounce_minutes"
MODE = "mode"
FIELDS = (MODE, INTERVAL, DEBOUNCE)

DEFAULTS: dict[str, str | int] = {
    MODE: DEFAULT_INDEXING_MODE,
    INTERVAL: DEFAULT_INDEX_INTERVAL_MINUTES,
    DEBOUNCE: DEFAULT_INDEX_DEBOUNCE_MINUTES,
}
BOUNDS: dict[str, tuple[int, int]] = {
    INTERVAL: (MIN_INDEX_INTERVAL_MINUTES, MAX_INDEX_INTERVAL_MINUTES),
    DEBOUNCE: (MIN_INDEX_DEBOUNCE_MINUTES, MAX_INDEX_DEBOUNCE_MINUTES),
}


@dataclass(frozen=True)
class Schedule:
    """What a project does, and where each field of it was decided."""

    mode: str
    interval_minutes: int
    debounce_minutes: int
    origins: dict[str, Origin]

    @property
    def watched(self) -> bool:
        """Whether the tree is watched rather than swept on a timer."""
        return self.mode == "auto"


def clean_mode(value: object) -> str | None:
    """Return a stored mode fit to act on, or None when it names none."""
    if isinstance(value, str) and value in INDEXING_MODES:
        return value
    LOG.warning("indexing mode %r is not one of %s", value, list(INDEXING_MODES))
    return None


def clean_minutes(field: str, value: object) -> int | None:
    """Return a stored duration clamped into its bounds, or None if it is not one.

    These rows are editable in psql as well as in the dashboard, so a number is
    clamped and reported rather than trusted: an interval of zero would ask for
    a run on every tick, and the scheduler would give it one.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        LOG.warning("indexing %s is %r, which is not a whole number", field, value)
        return None
    low, high = BOUNDS[field]
    clamped = max(low, min(high, value))
    if clamped != value:
        LOG.warning("indexing %s of %d clamped to %d", field, value, clamped)
    return clamped


def clean(field: str, value: object) -> str | int | None:
    """Return a stored value fit to act on, whichever field it belongs to."""
    if field == MODE:
        return clean_mode(value)
    return clean_minutes(field, value)


def resolve(cursor: Cursor, project: str) -> Schedule:
    """Settle a project's schedule, and say where each field came from.

    Every field is resolved on its own, as the two selection documents are: a
    project may set `auto` while the interval behind its fallback sweep is
    still the global one.
    """
    found: dict[str, tuple[Origin, str | int]] = {}
    for origin, name in levels(cursor, project):
        stored = read_settings_json(cursor, name).get(INDEXING_KEY)
        if not isinstance(stored, dict):
            continue
        for field in FIELDS:
            if field in found or stored.get(field) is None:
                continue
            value = clean(field, stored[field])
            if value is not None:
                found[field] = (origin, value)
    settled = {
        field: found.get(field, ("default", DEFAULTS[field])) for field in FIELDS
    }
    return Schedule(
        mode=settled[MODE][1],
        interval_minutes=settled[INTERVAL][1],
        debounce_minutes=settled[DEBOUNCE][1],
        origins={field: settled[field][0] for field in FIELDS},
    )


def due(
    schedule: Schedule,
    last_run: datetime | None,
    dirty: bool,
    now: datetime,
) -> str | None:
    """Say why a run is owed, or None when none is.

    A project never indexed is owed one as soon as it is not `off`: there is no
    last run to wait an interval from, and the graph is empty until it happens.
    """
    if schedule.mode == "off":
        return None
    since = None if last_run is None else now - last_run
    if schedule.mode == "auto" and dirty:
        if since is None or since >= timedelta(minutes=schedule.debounce_minutes):
            return "changed"
    if since is None or since >= timedelta(minutes=schedule.interval_minutes):
        # In `auto` this is the sweep that keeps a project indexed when the
        # watch is blind: a tree on a filesystem inotify says nothing about,
        # or a watch the kernel refused for want of `max_user_watches`.
        return "periodic" if schedule.mode == "periodic" else "fallback"
    return None


def next_due(schedule: Schedule, last_run: datetime | None) -> datetime | None:
    """When the sweep would next start a run, ignoring any change to come."""
    if schedule.mode == "off":
        return None
    if last_run is None:
        return None
    return last_run + timedelta(minutes=schedule.interval_minutes)
