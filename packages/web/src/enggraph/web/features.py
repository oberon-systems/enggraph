"""Read a background feature out of a request body, and hide its token."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from enggraph.core import jsjson
from enggraph.web.args import (
    MISSING,
    bad_request,
    field,
    read_body_number,
    read_body_string,
)

# The switchable features, as enggraph.core.config names them.
FEATURES = ("indexing", "summarize", "embedding")

# The same bounds enggraph.core.features clamps to when it reads a row.
NUMBERS = {
    "batch": (1, 64),
    "tick_seconds": (1, 3600),
    "budget_seconds": (5, 3600),
    "chunk_chars": (200, 20000),
    "chunk_overlap": (0, 200),
}
# Only the loop reads these, and the loop reads the global level.
ROOT_ONLY = {"tick_seconds", "budget_seconds"}
# The other copy of this number is enggraph.core.config.FEATURE_KEY_TTL_DAYS.
KEY_TTL_DAYS = 30
BAD_URL = 'Field "server_url" must be an http or https URL'


def require_feature(value: str) -> str:
    """Name a feature, or refuse: the path segment reaches the database."""
    if value not in FEATURES:
        raise bad_request(f'Unknown feature "{value}", one of {", ".join(FEATURES)}')
    return value


def _server_url(body: object) -> str | None:
    if field(body, "server_url") is None:
        return ""
    value = read_body_string(body, "server_url")
    if value is None:
        return None
    url = value.strip().removesuffix("/")
    if url == "":
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise bad_request(BAD_URL) from error
    if parsed.scheme not in ("http", "https") or parsed.netloc == "":
        raise bad_request(BAD_URL)
    return url


def _switch(body: object, name: str) -> Any:  # noqa: ANN401
    value = field(body, name)
    if value is MISSING or value is None:
        return value
    if not isinstance(value, bool):
        raise bad_request(f'Field "{name}" must be true or false')
    return value


def read_feature(body: object, root: bool = False) -> dict[str, Any] | None:
    """Return the feature a body states, or None when it states nothing.

    A null field is this level letting go of it; an absent one leaves what is
    stored alone.
    """
    if not isinstance(body, dict | list):
        raise bad_request("A JSON object body is required")
    value: dict[str, Any] = {}
    # A project has nothing to say about `allowed`, so it is dropped there.
    allowed = _switch(body, "allowed")
    if root and isinstance(allowed, bool):
        value["allowed"] = allowed
    enabled = _switch(body, "enabled")
    if enabled is not MISSING:
        value["enabled"] = enabled
    url = _server_url(body)
    if url is not None:
        value["server_url"] = None if url == "" else url
    for name, (low, high) in NUMBERS.items():
        if (not root and name in ROOT_ONLY) or field(body, name) is MISSING:
            continue
        value[name] = read_body_number(body, name, low, high)
    # Write-only: an absent or empty field leaves the stored token alone.
    key = (read_body_string(body, "server_key") or "").strip()
    if key != "":
        value["server_key"] = key
        value["key_saved_at"] = jsjson.instant(datetime.now(UTC))
    return value or None


def _saved(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def redact_keys(settings: object) -> dict[str, Any] | None:
    """Take the tokens out of a settings object before a browser sees it.

    A reader needs to know whether a token is stored and whether it is due for
    rotation, and neither is the secret.
    """
    if not isinstance(settings, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in settings.items():
        if not isinstance(value, dict):
            out[key] = value
            continue
        feature = dict(value)
        stored = feature.pop("server_key", None)
        if isinstance(stored, str) and stored != "":
            saved = _saved(feature.get("key_saved_at"))
            due = None if saved is None else saved + timedelta(days=KEY_TTL_DAYS)
            feature["key_set"] = True
            feature["key_due"] = None if due is None else jsjson.instant(due)
            feature["key_expired"] = due is not None and due < datetime.now(UTC)
        out[key] = feature
    return out
