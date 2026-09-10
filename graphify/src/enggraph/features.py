"""Whether a background feature runs for a project, and where that was decided.

A feature lives beside the selection and the schedule, in
`project_settings.settings`, and its fields resolve the way a schedule's do:
the project, then the organizations holding it, then the global default, most
specific first and one field at a time.

One field does not resolve that way. `enabled` at the global level is a gate
rather than a default: turned off there, it is off for every project whatever
their own rows say. That is what makes the global toggle something an operator
can trust - the model can be shut down and the queue stops, without walking
every project first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from psycopg2.extensions import cursor as Cursor

from enggraph.config import (
    FEATURE_DEFAULTS,
    FEATURE_KEY_TTL_DAYS,
    SETTINGS_PROJECT,
)
from enggraph.selection import Origin, levels
from enggraph.storage import read_settings_json

LOG = logging.getLogger(__name__)

# Whether the feature may run at all. Read from the global level and from
# nowhere else: allowing something is not a permission a project grants
# itself. It is deliberately apart from `enabled` - one field doing both jobs
# meant that turning a feature off by default also stopped any project from
# turning itself on, which is the opposite of a default.
ALLOWED = "allowed"
ENABLED = "enabled"
SERVER_URL = "server_url"
# The token a published server wants, and when it was last written. The pair
# travels together: a key with no date could not be reported as due for
# rotation, which is the only thing the date is for.
SERVER_KEY = "server_key"
KEY_SAVED_AT = "key_saved_at"
# How fast the queue behind this feature is worked. `batch` is per project -
# how many files one claim takes, which is how one project is given more of
# the machine than another. The other two are the loop's own and are read
# from the global level: a poll interval and a work budget belong to the
# process, not to whichever project it happens to be draining.
BATCH = "batch"
TICK_SECONDS = "tick_seconds"
BUDGET_SECONDS = "budget_seconds"
# How a file of this project is cut up before it is embedded. A tree of terse
# configuration files and one of long source files are not well served by the
# same window, which is why this is a project's setting rather than the
# stack's. The size is recorded on every chunk, so changing it re-embeds the
# project rather than leaving the old cut in place.
CHUNK_CHARS = "chunk_chars"
CHUNK_OVERLAP = "chunk_overlap"
NUMBERS = (BATCH, TICK_SECONDS, BUDGET_SECONDS, CHUNK_CHARS, CHUNK_OVERLAP)
FIELDS = (ENABLED, SERVER_URL, SERVER_KEY, KEY_SAVED_AT, *NUMBERS)
# Clamped rather than refused when read, as a schedule's minutes are: these
# rows are editable in psql, and a batch of zero would spin a loop.
BOUNDS: dict[str, tuple[int, int]] = {
    BATCH: (1, 64),
    TICK_SECONDS: (1, 3600),
    BUDGET_SECONDS: (5, 3600),
    CHUNK_CHARS: (200, 20000),
    CHUNK_OVERLAP: (0, 200),
}


@dataclass(frozen=True)
class Feature:
    """What a feature does for one project, and where each field came from."""

    name: str
    allowed: bool
    enabled: bool
    server_url: str
    origins: dict[str, Origin]
    # Everything below has a default so that a caller building one by hand -
    # a test, mostly - says only what it cares about, and a field added here
    # does not break every such caller.
    server_key: str = ""
    key_saved_at: str = ""
    batch: int = 1
    tick_seconds: int = 30
    budget_seconds: int = 60
    chunk_chars: int = 1500
    chunk_overlap: int = 5

    @property
    def gated(self) -> bool:
        """Whether the feature is off because it is disabled everywhere.

        Not the same as being off by default: a project may switch itself on
        under that, and under this it is not asked at all.
        """
        return not self.allowed

    @property
    def key_expired(self) -> bool:
        """Whether the stored key is older than the rotation window.

        Advisory, and deliberately so: the key goes on being sent. A queue
        that stopped itself because a date passed would be the silent failure
        this stack keeps being asked to avoid - the dashboard says the key is
        due for renewal, and the server is the one that decides whether it
        still works.
        """
        due = self.key_due()
        return due is not None and due < datetime.now(UTC)

    def key_due(self) -> datetime | None:
        """When the stored key should be renewed, or None when there is none."""
        if not self.server_key or not self.key_saved_at:
            return None
        try:
            saved = datetime.fromisoformat(self.key_saved_at.replace("Z", "+00:00"))
        except ValueError:
            LOG.warning("feature key_saved_at %r is not a date", self.key_saved_at)
            return None
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=UTC)
        return saved + timedelta(days=FEATURE_KEY_TTL_DAYS)


def clean_enabled(value: object) -> bool | None:
    """Return a stored switch fit to act on, or None when it names none.

    An absent field is not a complaint: it is how a level declines to answer,
    and warning about it filled the log with a line per project per tick.
    """
    if isinstance(value, bool):
        return value
    if value is not None:
        LOG.warning("feature switch is %r, which is not true or false", value)
    return None


def clean_url(value: object) -> str | None:
    """Return a stored server URL fit to dial, or None when it is not one.

    Refused here as well as in the dashboard: these rows are editable in psql,
    and a URL with no scheme reaches urllib as a relative path and fails a
    batch at a time rather than at the point it was typed.
    """
    if not isinstance(value, str):
        LOG.warning("feature server_url is %r, which is not a string", value)
        return None
    url = value.strip().rstrip("/")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        LOG.warning("feature server_url %r is not an http or https URL", value)
        return None
    return url


def clean_text(field: str, value: object) -> str | None:
    """Return a stored string fit to use, or None when it is not one."""
    if not isinstance(value, str):
        LOG.warning("feature %s is %r, which is not a string", field, value)
        return None
    return value.strip()


def clean_number(field: str, value: object) -> int | None:
    """Return a stored count clamped into its bounds, or None if it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        LOG.warning("feature %s is %r, which is not a whole number", field, value)
        return None
    low, high = BOUNDS[field]
    clamped = max(low, min(high, value))
    if clamped != value:
        LOG.warning("feature %s of %d clamped to %d", field, value, clamped)
    return clamped


def clean(field: str, value: object) -> bool | str | int | None:
    """Return a stored value fit to act on, whichever field it belongs to."""
    if field == ENABLED:
        return clean_enabled(value)
    if field == SERVER_URL:
        return clean_url(value)
    if field in NUMBERS:
        return clean_number(field, value)
    return clean_text(field, value)


def stored(cursor: Cursor, project: str, feature: str) -> dict:
    """Read one level's object for a feature, empty when it says nothing."""
    value = read_settings_json(cursor, project).get(feature)
    return value if isinstance(value, dict) else {}


def resolve(cursor: Cursor, project: str, feature: str) -> Feature:
    """Settle a feature for one project, and say where each field came from.

    `allowed` is read first, from the global level, and answered from at once
    when it says no. Everything else is the schedule's walk: the first level
    that states a field decides it, and a level that states nothing is skipped
    rather than treated as a no - which is what lets a project run a feature
    the global default leaves off.
    """
    defaults = FEATURE_DEFAULTS[feature]
    allowed = clean_enabled(stored(cursor, SETTINGS_PROJECT, feature).get(ALLOWED))
    if allowed is False:
        return Feature(
            name=feature,
            allowed=False,
            enabled=False,
            server_url="",
            server_key="",
            key_saved_at="",
            batch=int(FEATURE_DEFAULTS[feature][BATCH]),
            tick_seconds=int(FEATURE_DEFAULTS[feature][TICK_SECONDS]),
            budget_seconds=int(FEATURE_DEFAULTS[feature][BUDGET_SECONDS]),
            chunk_chars=int(FEATURE_DEFAULTS[feature][CHUNK_CHARS]),
            chunk_overlap=int(FEATURE_DEFAULTS[feature][CHUNK_OVERLAP]),
            origins=dict.fromkeys(FIELDS, "global"),
        )

    found: dict[str, tuple[Origin, bool | str | int]] = {}
    for origin, name in levels(cursor, project):
        level = stored(cursor, name, feature)
        for field in FIELDS:
            if field in found or level.get(field) is None:
                continue
            value = clean(field, level[field])
            if value is not None and value != "":
                found[field] = (origin, value)
    settled = {
        field: found.get(field, ("default", defaults[field])) for field in FIELDS
    }
    return Feature(
        name=feature,
        allowed=True,
        enabled=bool(settled[ENABLED][1]),
        server_url=str(settled[SERVER_URL][1]),
        server_key=str(settled[SERVER_KEY][1]),
        key_saved_at=str(settled[KEY_SAVED_AT][1]),
        batch=int(settled[BATCH][1]),
        tick_seconds=int(settled[TICK_SECONDS][1]),
        budget_seconds=int(settled[BUDGET_SECONDS][1]),
        chunk_chars=int(settled[CHUNK_CHARS][1]),
        chunk_overlap=int(settled[CHUNK_OVERLAP][1]),
        origins={field: settled[field][0] for field in FIELDS},
    )
