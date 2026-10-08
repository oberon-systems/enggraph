"""What the templates say about a value: an age, a share, a state."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

from enggraph.web.args import URI_SAFE

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
STALE_AFTER_DAYS = 7
# A project that indexes itself is expected to be minutes old.
INDEXED_WARN_SECONDS = 30 * MINUTE
INDEXED_LATE_SECONDS = HOUR

# One path each, drawn on a 16x16 grid.
ICONS = {
    "drop": "M4 4l8 8M12 4l-8 8",
    "settings": (
        "M8 5.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5M8 1.5v2M8 12.5v2M2.5 8h2M11.5 8h2"
        "M4.1 4.1l1.4 1.4M10.5 10.5l1.4 1.4M11.9 4.1l-1.4 1.4M5.5 10.5l-1.4 1.4"
    ),
    "index": "M4 3l8 5-8 5z",
    "fresh": "M13 8a5 5 0 11-1.7-3.8M13 2v3h-3",
    "retry": "M3 8a5 5 0 101.7-3.8M3 2v3h3",
    "running": "M8 2.5a5.5 5.5 0 105.5 5.5",
    "level": "M8 2l6 3-6 3-6-3zM2 8l6 3 6-3M2 11.5l6 3 6-3",
    "periodic": "M8 2a6 6 0 100 12A6 6 0 008 2M8 4.5V8l2.5 1.5",
    "auto": (
        "M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8"
        "M8 6a2 2 0 100 4 2 2 0 000-4"
    ),
}

# Where a schedule was decided, in the words the settings page uses for it.
SCHEDULE_LEVELS = {
    "project": "set on the project",
    "organization": "set on an organization this project is part of",
    "global": "set as the global default",
    "default": "nothing set one",
}
RANK = {"down": 0, "unknown": 1, "ok": 2}

_markdown = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def markdown(text: str | None) -> Markup:
    """Render markdown an agent wrote, cleaned: the database is no boundary."""
    return Markup(nh3.clean(_markdown.render(text or "")))  # noqa: S704


def thousands(value: float | None) -> str:
    """Spell a number with its thousands apart."""
    return f"{value or 0:,}"


def slug(value: object) -> str:
    """Turn a name into something an element id and a selector both carry."""
    return re.sub(r"[^A-Za-z0-9]+", "-", str(value))


def component(value: object) -> str:
    """Escape a value for one path segment or one query value."""
    return quote(str(value), safe=URI_SAFE)


def address(path: str, **params: object) -> str:
    """Build an address, leaving out the parameters that say nothing."""
    kept = {key: value for key, value in params.items() if value not in (None, "")}
    return f"{path}?{urlencode(kept, quote_via=quote)}" if kept else path


def go(path: str, name: str, **params: object) -> str:
    """Build the address a select navigates to, `{value}` standing for its choice."""
    base = address(path, **{key: value for key, value in params.items() if key != name})
    return f"{base}{'&' if '?' in base else '?'}{name}={{value}}"


def age(seconds: float) -> str:
    """Say how long ago, in the coarsest unit that still says something."""
    if seconds < MINUTE:
        return "just now"
    if seconds < HOUR:
        return f"{math.floor(seconds / MINUTE)} min ago"
    if seconds < DAY:
        hours = math.floor(seconds / HOUR)
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = math.floor(seconds / DAY)
    return f"{days} day{'' if days == 1 else 's'} ago"


def minutes(count: int) -> str:
    """Spell a duration the schedule stores, which is a count of minutes."""
    return f"{count} minute{'' if count == 1 else 's'}"


def freshness(stale_seconds: float | None) -> str:
    """Grade an age on the scale of days, for projects indexed by hand."""
    if stale_seconds is None:
        return "stale"
    return "stale" if stale_seconds > STALE_AFTER_DAYS * DAY else "fresh"


def indexed_grade(stale_seconds: float) -> str:
    """Grade an age on the scale of an hour, for projects indexing themselves."""
    if stale_seconds < INDEXED_WARN_SECONDS:
        return "fresh"
    return "stale" if stale_seconds < INDEXED_LATE_SECONDS else "overdue"


def schedule_detail(schedule: dict[str, Any]) -> str:
    """Say what one schedule does, in the words a hover has room for."""
    if schedule["mode"] == "off":
        return "only the Index button starts a run"
    if schedule["mode"] == "periodic":
        return f"a run every {minutes(schedule['interval_minutes'])}"
    return (
        "watching the tree, at most once every "
        f"{minutes(schedule['debounce_minutes'])}, swept every "
        f"{minutes(schedule['interval_minutes'])}"
    )


def schedule_level(origin: str) -> str:
    """Say where a schedule was decided."""
    return SCHEDULE_LEVELS.get(origin, origin)


def percent_of(done: int, total: int) -> int | None:
    """Return a share as a whole percent, never 100 while one is missing."""
    if total == 0:
        return None
    share = math.floor(done / total * 100)
    return 99 if share == 100 and done < total else share


def waiting_of(queue: dict[str, int]) -> int:
    """Count the files still owed work: queued, or held by a worker."""
    return (
        (queue.get("pending") or 0)
        + (queue.get("running") or 0)
        + (queue.get("leased") or 0)
    )


def coverage_tone(done: int, total: int, waiting: int, failed: int) -> str | None:
    """Pick the colour of a queue's percent: done, working, or stopped short."""
    if total > 0 and done >= total:
        return "coverage-done"
    if waiting > 0:
        return "coverage-busy"
    return "coverage-failed" if failed > 0 else None


def embedding_badge(embedding: dict[str, Any]) -> dict[str, str]:
    """Return the state, the text and the title of a project's vectors."""
    percent = percent_of(embedding["files"], embedding["indexed_files"])
    queue = embedding["queue"]
    waiting = (queue.get("pending") or 0) + (queue.get("running") or 0)
    failed = queue.get("failed") or 0
    title = f"{embedding['files']} of {embedding['indexed_files']} file(s) embedded"
    title += ", nothing queued" if waiting == 0 else f", {waiting} queued"
    title += "" if failed == 0 else f", {failed} failed"
    return {
        "state": "done" if percent == 100 and waiting == 0 else "filling",
        "text": "-" if percent is None else f"{percent}%",
        "title": title,
    }


def when(stamp: str | None) -> str:
    """Spell a moment for a reader, or say there was none."""
    if stamp is None:
        return "never"
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%d/%m/%Y, %H:%M:%S UTC")


def day(stamp: str | None) -> str:
    """Spell the day of a moment, or say none was recorded."""
    return "no date recorded" if stamp is None else when(stamp).split(",")[0]


def inherits(value: object, origin: str | None) -> str:
    """Say what an empty box gets: the value it inherits, and from where."""
    if value is None or value == "":
        return "inherited: nothing set"
    if origin in ("default", "environment"):
        return f"{origin}: {value}"
    if origin is None:
        return f"inherited: {value}"
    return f"inherited from {origin}: {value}"


def placeholder(settled: dict[str, Any] | None, field: str) -> str:
    """Return the placeholder of one field of a feature, resolved above it."""
    settled = settled or {}
    above = settled.get("inherited") or {}
    value = above.get(field)
    value = settled.get(field) if value is None else value
    origin = (above.get("origins") or {}).get(field)
    if origin is None:
        origin = (settled.get("origins") or {}).get(field)
    return inherits(value, origin)


def url_in_force(settled: dict[str, Any] | None, own: bool) -> str:
    """Say which server a level dials, and whether it chose it or inherited it."""
    settled = settled or {}
    above = settled.get("inherited") or {}
    url = settled.get("server_url") or above.get("server_url") or ""
    if url == "":
        return "No server URL in force: nothing is dialled from here."
    if own:
        return f"URL in force: {url} (set on this level)"
    origins = settled.get("origins") if settled.get("server_url") else None
    origins = origins if origins is not None else above.get("origins")
    origin = (origins or {}).get("server_url") or "the level above"
    return f"URL in force: {url} (inherited from {origin})"


def describe_server(server: dict[str, Any]) -> str:
    """Say what a server last did."""
    where = server["url"] or "no server URL"
    if server["state"] == "ok":
        return f"{where} answers (checked {when(server.get('checked_at'))})"
    if server["state"] == "down":
        since = when(server.get("since"))
        return f"{where} does not answer since {since}: {server.get('reason')}"
    return f"{where}: {server.get('reason')}"


def enabled_rows(
    rows: list[dict[str, Any]] | None, project: str | None = None
) -> list[dict[str, Any]]:
    """Keep the rows of the enabled projects, or of the one named."""
    return [
        row
        for row in rows or []
        if row["enabled"] and (project is None or row["project"] == project)
    ]


def addresses(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group rows by the server they dial, the one that is down first."""
    by_url: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_url.setdefault(
            row["server"]["url"], {"server": row["server"], "projects": []}
        )
        entry["projects"].append(row["project"])
    return sorted(by_url.values(), key=lambda one: RANK[one["server"]["state"]])


def worst(rows: list[dict[str, Any]]) -> str:
    """Return the worst state among the servers of some rows."""
    if any(row["server"]["state"] == "down" for row in rows):
        return "down"
    return "ok" if all(row["server"]["state"] == "ok" for row in rows) else "unknown"


def lamp(rows: list[dict[str, Any]], counted: bool) -> dict[str, str] | None:
    """Return the state and the title of one lamp, or None when it is unlit."""
    if not rows:
        return None
    lines = []
    for one in addresses(rows):
        line = describe_server(one["server"])
        if counted:
            line += f" - {len(one['projects'])} project(s)"
        lines.append(line)
    return {"state": worst(rows), "title": "\n".join(lines)}


def project_entries(
    targets: list[dict[str, Any]], tagged: list[str] | None = None
) -> list[dict[str, str]]:
    """List organizations with their members, then the projects in none."""
    known = {target["name"] for target in targets}
    entries: list[dict[str, str]] = []
    for organization in targets:
        if organization["type"] != "organization":
            continue
        name = organization["name"]
        entries.append(
            {"value": name, "label": f"{name}, whole organization", "group": name}
        )
        entries.extend(
            {"value": member["name"], "label": member["name"], "group": name}
            for member in targets
            if name in member["organizations"]
        )
    entries.extend(
        {"value": target["name"], "label": target["name"], "group": "Projects"}
        for target in targets
        if target["type"] != "organization" and not target["organizations"]
    )
    entries.extend(
        {"value": name, "label": name, "group": "No longer a project"}
        for name in tagged or []
        if name not in known
    )
    return entries


HELPERS = {
    "ICONS": ICONS,
    "address": address,
    "addresses": addresses,
    "age": age,
    "component": component,
    "coverage_tone": coverage_tone,
    "day": day,
    "describe_server": describe_server,
    "embedding_badge": embedding_badge,
    "enabled_rows": enabled_rows,
    "freshness": freshness,
    "go": go,
    "indexed_grade": indexed_grade,
    "markdown": markdown,
    "minutes": minutes,
    "percent_of": percent_of,
    "placeholder": placeholder,
    "schedule_detail": schedule_detail,
    "schedule_level": schedule_level,
    "slug": slug,
    "thousands": thousands,
    "url_in_force": url_in_force,
    "waiting_of": waiting_of,
    "when": when,
}
