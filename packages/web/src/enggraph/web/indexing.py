"""Read a schedule out of a request body."""

from __future__ import annotations

from typing import Any

from enggraph.web.args import (
    MISSING,
    bad_request,
    field,
    read_body_enum,
    read_body_number,
)

# The key `settings.settings` holds a schedule under, as enggraph.core.config
# spells it. The bounds are the ones enggraph.api.schedule clamps to.
INDEXING_KEY = "indexing"
INDEXING_MODES = ("off", "periodic", "auto")
MIN_INTERVAL = 1
MAX_INTERVAL = 10080
MIN_DEBOUNCE = 1
MAX_DEBOUNCE = 1440


def _switch(body: object, name: str) -> bool | None:
    value = field(body, name)
    if value is MISSING or value is None:
        return None
    if not isinstance(value, bool):
        raise bad_request(f'Field "{name}" must be true or false')
    return value


def read_indexing(body: object, root: bool = False) -> dict[str, Any] | None:
    """Return the schedule a body states, or None when it states nothing.

    A field left out inherits from the level above, so a body that leaves out
    all of them is stored by removing the key.
    """
    value: dict[str, Any] = {}
    allowed = _switch(body, "allowed") if root else None
    if allowed is not None:
        value["allowed"] = allowed
    mode = read_body_enum(body, "mode", INDEXING_MODES)
    if mode is not None:
        value["mode"] = mode
    interval = read_body_number(body, "interval_minutes", MIN_INTERVAL, MAX_INTERVAL)
    if interval is not None:
        value["interval_minutes"] = interval
    debounce = read_body_number(body, "debounce_minutes", MIN_DEBOUNCE, MAX_DEBOUNCE)
    if debounce is not None:
        value["debounce_minutes"] = debounce
    enabled = _switch(body, "enabled")
    if enabled is not None:
        value["enabled"] = enabled
    return value or None
