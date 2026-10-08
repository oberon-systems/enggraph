"""Projects: the listing, one project, its settings, and what changes them."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter
from starlette.responses import Response

from enggraph.core import db, jsjson
from enggraph.web import queries as sql
from enggraph.web.args import (
    Body,
    HttpError,
    bad_request,
    count,
    field,
    not_found,
    read_body_string,
    reply,
    text,
)
from enggraph.web.features import read_feature, redact_keys, require_feature
from enggraph.web.indexing import INDEXING_KEY, read_indexing
from enggraph.web.upstream import UpstreamError, ask, call, segment

log = logging.getLogger(__name__)

# The vocabulary of enggraph.core.config.KNOWN_PROJECT_TYPES: the column is
# unconstrained, so this is where a dashboard write is held to it.
PROJECT_TYPES = ("codebase", "docs", "config", "organization")
# Types holding records an agent wrote: an index run into one deletes them all.
BUILTIN_TYPES = {"memory", "plans", "suggestions", "settings"}
DESCRIPTION_LIMIT = 500
# The built-in project the global defaults hang off.
SETTINGS_PROJECT = "_settings"
# How long the listing waits on the API before rendering from what it last heard.
LISTING_UPSTREAM_MS = 2000
DROP_MS = 600_000

router = APIRouter()
_last_heard: dict[str, list[Any]] = {}
_asking = ThreadPoolExecutor(max_workers=6, thread_name_prefix="listing")


def _project(row: dict[str, Any]) -> dict[str, Any]:
    stale = row["stale_seconds"]
    return {
        "name": row["name"],
        "type": row["type"],
        "description": row["description"],
        "root_path": row["root_path"],
        "indexed_at": row["indexed_at"],
        "stale_seconds": None if stale is None else float(stale),
        "nodes": count(row["nodes"]),
        "edges": count(row["edges"]),
        "files": count(row["files"]),
        "plans": count(row["plans"]),
        "members": count(row["members"]),
    }


def _drop_report(name: str, row: dict[str, Any], dropped: bool) -> dict[str, Any]:
    counted = (
        "nodes",
        "edges",
        "hashes",
        "embeddings",
        "plans",
        "suggestions",
        "summaries",
        "relations",
        "exports",
        "record_links",
    )
    return {
        "name": name,
        "root_path": row["root_path"],
        "indexed_at": row["indexed_at"],
        **{key: count(row[key]) for key in counted},
        "dropped": dropped,
    }


def require_project(name: str) -> str:
    """Return the name of a project that exists, or answer 404."""
    if not db.query(sql.PROJECT_EXISTS, [name]):
        raise not_found(f'No indexed project named "{name}"')
    return name


def _under(name: str, rest: str = "") -> str:
    return f"/projects/{segment(name)}{rest}"


def _plural(members: int) -> str:
    return f"{members} project{'' if members == 1 else 's'}"


def start_index(name: str, body: object) -> Any:  # noqa: ANN401
    """Ask the API to index a project."""
    name = require_project(name)
    fresh = field(body, "fresh") is True
    return ask("POST", "/index", None, {"project": name, "fresh": fresh})


def index_state(name: str) -> Any:  # noqa: ANN401
    """Say how a project last indexed, folded over an organization."""
    return ask("GET", _under(require_project(name), "/index"))


def members(name: str) -> Any:  # noqa: ANN401
    """Say what an organization holds."""
    return ask("GET", _under(require_project(name), "/members"))


def organizations(name: str) -> dict[str, Any]:
    """Say what holds a project, and which of them owns it."""
    name = require_project(name)
    rows = db.query(sql.PROJECT_ORGANIZATIONS, [name])
    owner = next((row["organization"] for row in rows if row["owned"]), None)
    return {
        "project": name,
        "organizations": [row["organization"] for row in rows],
        # The one it was moved into is where the project is listed instead.
        "owner": owner,
    }


def set_organizations(name: str, body: object) -> Any:  # noqa: ANN401
    """Settle where a project belongs."""
    name = require_project(name)
    listed = field(body, "organizations")
    wanted = [text(one) for one in listed] if isinstance(listed, list) else []
    return ask("PUT", _under(name, "/organizations"), None, {"organizations": wanted})


def add_member(name: str, body: object) -> Any:  # noqa: ANN401
    """Add a project to an organization."""
    name = require_project(name)
    wanted = field(body, "project")
    member = require_project("" if wanted is None or not wanted else text(wanted))
    return ask("POST", _under(name, "/members"), None, {"project": member})


def drop_member(name: str, member: str) -> Any:  # noqa: ANN401
    """Take a project out of an organization."""
    name = require_project(name)
    return ask("DELETE", _under(name, f"/members/{segment(member)}"))


def rename(name: str, body: object) -> Any:  # noqa: ANN401
    """Change the name a project is addressed by."""
    name = require_project(name)
    wanted = read_body_string(body, "project")
    if wanted is None or wanted.strip() == "":
        raise bad_request('Send {"project": "the name it takes"}')
    return ask("POST", _under(name, "/rename"), None, {"project": wanted.strip()})


def register(body: object) -> Any:  # noqa: ANN401
    """Register a project: the row first, the mount on the host after."""
    return ask(
        "POST",
        "/projects",
        None,
        {
            "name": read_body_string(body, "name") or "",
            "root_path": read_body_string(body, "root_path") or "",
            "project_type": read_body_string(body, "type") or "",
        },
    )


def patch(name: str, body: object) -> dict[str, Any]:
    """Change what a project is, or what it says it is for."""
    name = require_project(name)
    kind = read_body_string(body, "type")
    description = read_body_string(body, "description")
    if kind is None and description is None:
        raise bad_request(
            'Send {"type": "codebase" | "docs" | "config"} or '
            '{"description": "what this project is for"}'
        )
    if description is not None:
        written = description.strip()
        if len(written) > DESCRIPTION_LIMIT:
            raise bad_request(
                f"A description is {DESCRIPTION_LIMIT} characters at most, and "
                f"this one is {len(written)}. It is read in a list beside "
                "every other project, not instead of the README."
            )
        # An emptied field is no description rather than an empty one.
        db.query(sql.PATCH_PROJECT_DESCRIPTION, [name, written or None])
    if kind is None:
        return db.query(sql.PROJECT_IDENTITY, [name])[0]
    if kind in BUILTIN_TYPES:
        raise HttpError(
            409,
            f'"{kind}" holds records written by an agent rather than an indexed '
            "tree, and indexing into it would delete every one of them. Only "
            f"{', '.join(PROJECT_TYPES)} can be set here.",
        )
    if kind not in PROJECT_TYPES:
        raise bad_request(
            f'"{kind}" is not a project type. Expected one of '
            f"{', '.join(PROJECT_TYPES)}."
        )
    current = db.query(sql.PROJECT_TYPE, [name])[0]["type"]
    if current in BUILTIN_TYPES:
        raise HttpError(
            409,
            f"{name} holds agent {current} rather than an indexed "
            "tree. Its type is what keeps an index run out of it.",
        )
    if current == "organization" and kind != "organization":
        held = _holdings(name)
        if held > 0:
            raise HttpError(
                409,
                f'"{name}" holds {_plural(held)}. An '
                "organization that holds something stays one: take them out "
                "first, and it is free to be anything.",
            )
    db.query(sql.PATCH_PROJECT_TYPE, [name, kind])
    return db.query(sql.PROJECT_IDENTITY, [name])[0]


def _holdings(name: str) -> int:
    rows = db.query(sql.PROJECT_HOLDINGS, [name])
    return count(rows[0]["members"]) if rows else 0


def _last_known(path: str, key: str) -> list[Any]:
    """Ask the API for one list, or answer with the one it gave last time."""
    try:
        answer = call("GET", path, None, None, LISTING_UPSTREAM_MS)
        listed = answer.get(key) or []
        _last_heard[path] = listed
        return listed
    except (UpstreamError, ValueError, AttributeError) as reason:
        log.warning("%s is unavailable: %s", path, reason)
        return _last_heard.get(path, [])


def listing() -> dict[str, Any]:
    """List the projects with their counts, schedules and embedding state."""
    # These do not pass their failure on: the listing is the home page, and
    # renders while the API is recreated.
    asked = [
        _asking.submit(_last_known, path, key)
        for path, key in (
            ("/schedules", "schedules"),
            ("/embeddings", "embeddings"),
            ("/listing", "counts"),
        )
    ]
    rows = db.query(sql.PROJECTS)
    schedules, embeddings, cached = (one.result() for one in asked)
    counts = {one["project"]: one for one in cached}
    missing = [row["name"] for row in rows if row["name"] not in counts]
    if missing:
        for one in db.query(sql.PROJECT_COUNTS, [missing]):
            counts[one["name"]] = one
    folded = {one["project"]: one for one in schedules}
    embedded = {one["project"]: one for one in embeddings}
    items = []
    for row in rows:
        counted = counts.get(row["name"], {})
        items.append(
            {
                **_project(
                    {
                        **row,
                        "nodes": counted.get("nodes") or 0,
                        "edges": counted.get("edges") or 0,
                        "files": counted.get("files") or 0,
                    }
                ),
                "schedule": folded.get(row["name"]),
                "embedding": embedded.get(row["name"]),
            }
        )
    return {"items": items}


def detail(name: str) -> dict[str, Any]:
    """Describe one project: its counts by node type and by relation."""
    name = require_project(name)
    row = db.query(sql.PROJECT, [name])[0]
    types = db.query(sql.PROJECT_NODE_TYPES, [name])
    relations = db.query(sql.PROJECT_RELATIONS, [name])
    extras = db.query(sql.PROJECT_EXTRAS, [name])[0]
    return {
        **_project(row),
        "types": [{"type": r["type"], "count": count(r["count"])} for r in types],
        "relations": [
            {"relation_type": r["relation_type"], "count": count(r["count"])}
            for r in relations
        ],
        "manual_summaries": count(extras["manual_summaries"]),
        "summarised": count(extras["summarised"]),
        "hashed_files": count(extras["hashed_files"]),
        "embeddings": count(extras["embeddings"]),
    }


def _level(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    # The tokens never leave this process.
    if not rows:
        return None
    return {**rows[0], "settings": redact_keys(rows[0]["settings"])}


def settings(name: str) -> dict[str, Any]:
    """Say what a project prunes, and the ignore lines it inherits."""
    name = require_project(name)
    return {
        "project": _level(db.query(sql.PROJECT_SETTINGS, [name])),
        "global": _level(db.query(sql.PROJECT_LEVEL_SETTINGS, [SETTINGS_PROJECT])),
        "inherited": db.query(sql.PROJECT_INHERITED_IGNORE, [name, SETTINGS_PROJECT]),
    }


def file_types(name: str) -> dict[str, Any]:
    """Count the file types actually in the graph."""
    rows = db.query(sql.PROJECT_FILE_TYPES, [require_project(name)])
    return {
        "items": [
            {"extension": row["extension"], "count": count(row["count"])}
            for row in rows
        ]
    }


def save_ignore(name: str, body: object) -> dict[str, Any]:
    """Store a level's ignore document; an empty one is stored as NULL."""
    ignore = (read_body_string(body, "ignore_patterns") or "").strip()
    return db.query(sql.SAVE_IGNORE, [name, f"{ignore}\n" if ignore else None])[0]


def save_indexing(name: str, body: object, root: bool = False) -> dict[str, Any]:
    """Store when a level indexes itself, or let the level above answer."""
    value = read_indexing(body, root)
    if value is None:
        saved = db.query(sql.CLEAR_SETTINGS_KEY, [name, INDEXING_KEY])
    else:
        document = jsjson.dumps({INDEXING_KEY: value})
        saved = db.query(sql.SAVE_SETTINGS_KEY, [name, document])
    return saved[0] if saved else {"project": name}


def save_feature(
    name: str, feature: str, body: object, root: bool = False
) -> dict[str, Any]:
    """Store what a level does with a background feature."""
    feature = require_feature(feature)
    value = read_feature(body, root)
    if value is None:
        saved = db.query(sql.CLEAR_SETTINGS_KEY, [name, feature])
    else:
        saved = db.query(sql.MERGE_SETTINGS_KEY, [name, feature, jsjson.dumps(value)])
    return saved[0] if saved else {"project": name}


def passed(name: str, what: str, method: str = "GET") -> Any:  # noqa: ANN401
    """Ask the API something about a project and answer what it said."""
    return ask(method, _under(require_project(name), f"/{what}"))


def clear_settings(name: str) -> dict[str, Any]:
    """Drop everything a project says for itself."""
    name = require_project(name)
    dropped = db.query(sql.CLEAR_SETTINGS, [name])
    return dropped[0] if dropped else {"project": name}


def drop_report(name: str) -> dict[str, Any]:
    """Say what dropping a project would cost."""
    name = require_project(name)
    return _drop_report(name, db.query(sql.DROP_REPORT, [name])[0], False)


def drop(name: str, body: object) -> dict[str, Any]:
    """Drop a project, once the request confirms it."""
    name = require_project(name)
    if field(body, "confirm") is not True:
        raise HttpError(
            409,
            'Send {"confirm": true} to drop the project. Ask for the drop report '
            "first: the graph goes, and only indexing it again brings it back.",
        )
    kind = db.query(sql.PROJECT_TYPE, [name])
    if kind and kind[0]["type"] == "organization":
        held = _holdings(name)
        if held > 0:
            raise HttpError(
                409,
                f'"{name}" holds {_plural(held)}. '
                "Take them out before dropping it, so nothing is lost by a "
                "decision about something else.",
            )
    names = [row["organization"] for row in db.query(sql.PROJECT_ORGANIZATIONS, [name])]
    if names:
        those = "those organizations" if len(names) > 1 else "that organization"
        raise HttpError(
            409,
            f'"{name}" is part of {", ".join(names)}. Take it out of '
            f"{those} "
            "before dropping it: membership is a reference, and following it "
            "here would leave a search with a hole.",
        )
    # No table holds a key, so the API deletes from every table in one go.
    report = db.query(sql.DROP_REPORT, [name])[0]
    ask("POST", _under(name, "/drop"), None, {}, DROP_MS)
    return _drop_report(name, report, True)


@router.post("/projects/{name}/index")
def _start_index(name: str, body: Body) -> Response:
    return reply(start_index(name, body), 202)


@router.get("/projects/{name}/index")
def _index_state(name: str) -> Response:
    return reply(index_state(name))


@router.get("/projects/{name}/members")
def _members(name: str) -> Response:
    return reply(members(name))


@router.get("/projects/{name}/organizations")
def _organizations(name: str) -> Response:
    return reply(organizations(name))


@router.put("/projects/{name}/organizations")
def _set_organizations(name: str, body: Body) -> Response:
    return reply(set_organizations(name, body))


@router.post("/projects/{name}/members")
def _add_member(name: str, body: Body) -> Response:
    return reply(add_member(name, body), 201)


@router.delete("/projects/{name}/members/{member}")
def _drop_member(name: str, member: str) -> Response:
    return reply(drop_member(name, member))


@router.post("/projects/{name}/rename")
def _rename(name: str, body: Body) -> Response:
    return reply(rename(name, body))


@router.post("/projects")
def _register(body: Body) -> Response:
    return reply(register(body), 201)


@router.patch("/projects/{name}")
def _patch(name: str, body: Body) -> Response:
    return reply(patch(name, body))


@router.get("/projects")
def _listing() -> Response:
    return reply(listing())


@router.get("/projects/{name}")
def _detail(name: str) -> Response:
    return reply(detail(name))


@router.get("/projects/{name}/settings")
def _settings(name: str) -> Response:
    return reply(settings(name))


@router.get("/projects/{name}/file-types")
def _file_types(name: str) -> Response:
    return reply(file_types(name))


@router.put("/projects/{name}/settings")
def _save_ignore(name: str, body: Body) -> Response:
    return reply(save_ignore(require_project(name), body))


@router.put("/projects/{name}/indexing")
def _save_indexing(name: str, body: Body) -> Response:
    return reply(save_indexing(require_project(name), body))


@router.put("/projects/{name}/features/{feature}")
def _save_feature(name: str, feature: str, body: Body) -> Response:
    return reply(save_feature(require_project(name), feature, body))


@router.get("/projects/{name}/features")
def _features(name: str) -> Response:
    return reply(passed(name, "features"))


@router.get("/projects/{name}/failures")
def _failures(name: str) -> Response:
    return reply(passed(name, "failures"))


@router.get("/projects/{name}/schedule")
def _schedule(name: str) -> Response:
    return reply(passed(name, "schedule"))


@router.delete("/projects/{name}/settings")
def _clear_settings(name: str) -> Response:
    return reply(clear_settings(name))


@router.post("/projects/{name}/formats")
def _formats(name: str) -> Response:
    return reply(passed(name, "formats", "POST"))


@router.get("/projects/{name}/drop-report")
def _drop_report_route(name: str) -> Response:
    return reply(drop_report(name))


@router.delete("/projects/{name}")
def _drop(name: str, body: Body) -> Response:
    return reply(drop(name, body))
