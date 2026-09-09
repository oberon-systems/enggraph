"""Hand summarizing work to a machine that has a GPU and no copy of the tree.

The local pass loads a model in this container and reads the files off the
mount. A worker on another machine has neither, so the work becomes a queue:
it claims a batch, is handed the text and the prompt to run, and sends back
one sentence per file. Nothing it sends is trusted - the answer goes through
the same shaping and the same gates the in-process summarizer applies before
it reaches a node.

Every handler is a plain `def`. psycopg2 is synchronous, so an `async def`
here would block the event loop for every other request in flight.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg2
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from psycopg2.extensions import cursor as Cursor
from psycopg2.pool import ThreadedConnectionPool
from pydantic import BaseModel, Field

from enggraph import bootstrap, indexjobs, jobs, schedule, sources
from enggraph.config import (
    IGNORE_FILES,
    KEEP_FILES,
    KNOWN_PROJECT_TYPES,
    LLM_INPUT_CHARS,
    LLM_MAX_TOKENS,
    ORGANIZATION_PROJECT_TYPE,
    SCHEDULER_ENABLED,
    WORKER_API_DOCS,
    WORKER_API_PORT,
    WORKER_API_TOKEN,
    WORKER_API_TOKEN_MIN,
    WORKER_LEASE_SECONDS,
    WORKER_MAX_ATTEMPTS,
    WORKER_MAX_BATCH,
    WORKER_MAX_REPLY_CHARS,
)
from enggraph.discovery import present, to_spec
from enggraph.identifiers import (
    project_mount,
    project_name,
    source_alias,
    source_mount,
)
from enggraph.selection import resolve
from enggraph.storage import (
    absorb_project,
    add_member,
    add_source,
    describe_projects,
    detach_source,
    drop_member,
    drop_source,
    get_cached_summary,
    get_db_url,
    list_all_sources,
    list_members,
    list_memberships,
    list_owned,
    list_sources,
    move_source,
    put_cached_summary,
    register_project,
    registered_root,
    rename_project,
    save_llm_summary,
    set_memberships,
    source_owner,
    stored_type,
)
from enggraph.summary_text import (
    SYSTEM_PROMPT,
    content_key,
    shape,
    strip_preamble,
    useful,
)

LOG = logging.getLogger(__name__)

DEFAULT_PAGE = 50
MAX_PAGE = 500
NOT_USEFUL = "says nothing the file name does not"

_pool: ThreadedConnectionPool | None = None


def pool() -> ThreadedConnectionPool:
    """Return the connection pool, opening it on first use.

    A pool rather than a connection per request: this is a long-lived service,
    unlike every other user of `storage`, which is a job that runs once.
    """
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(1, 8, get_db_url())
    return _pool


@contextmanager
def transaction() -> Iterator[Cursor]:
    """Lend a connection out, committing what the handler wrote."""
    connection = pool().getconn()
    try:
        with connection.cursor() as cursor:
            yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        pool().putconn(connection)


def require_token(authorization: str = Header(default="")) -> None:
    """Refuse anything that does not carry the shared token.

    Both sides are encoded first: compare_digest raises TypeError on a string
    holding non-ASCII, which would turn a bad header into a 500.
    """
    scheme, _, offered = authorization.partition(" ")
    if scheme.lower() != "bearer" or not offered:
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(
        offered.encode("utf-8"), WORKER_API_TOKEN.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="bad token",
            headers={"WWW-Authenticate": "Bearer"},
        )


class IndexRequest(BaseModel):
    """What starting an index run needs to know."""

    project: str = Field(
        default="",
        description="the project to index; derived from root_path when unset",
    )
    root_path: str = Field(
        default="",
        description="host path of the tree; taken from the projects row when unset",
    )
    project_type: str = Field(
        default="", description="codebase, docs or config; keeps the stored one"
    )
    fresh: bool = Field(
        default=False, description="trust neither cache and parse every file"
    )
    alias: str = Field(
        default="",
        description=(
            "one directory of the project to walk, by the alias its node ids "
            "carry; every directory when unset"
        ),
    )


class JobRequest(BaseModel):
    """What to summarize, and how much of each file to show the model."""

    project: str
    refresh: bool = False
    input_chars: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    lease_seconds: int = Field(default=0, ge=0)
    model: str | None = None


class LeaseRequest(BaseModel):
    """A worker asking for its next batch."""

    worker_id: str = Field(min_length=1, max_length=64)
    batch: int = Field(default=4, ge=1)
    lease_seconds: int = Field(default=0, ge=0)


class LeaseHeld(BaseModel):
    """A worker naming the batch it holds."""

    worker_id: str = Field(min_length=1, max_length=64)
    lease_token: uuid.UUID
    lease_seconds: int = Field(default=0, ge=0)


class ResultRequest(BaseModel):
    """One file, described. `summary` is the raw reply, unshaped."""

    worker_id: str = Field(min_length=1, max_length=64)
    lease_token: uuid.UUID
    summary: str
    elapsed_ms: int | None = None
    model: str | None = None


class FailureRequest(BaseModel):
    """One file the worker could not describe."""

    worker_id: str = Field(min_length=1, max_length=64)
    lease_token: uuid.UUID
    error: str = ""


def apply_summary(
    cursor: Cursor, project: str, rel_path: str, summary: str
) -> tuple[bool, str | None]:
    """Put a shaped sentence on a node, if it says anything.

    A rejected answer is not applied but is still the answer, so the caller
    caches it either way: the files a small model is worst at are the ones it
    would otherwise be asked about on every pass.
    """
    if not useful(summary, rel_path):
        return False, NOT_USEFUL
    if not save_llm_summary(cursor, project, rel_path, summary):
        return False, "node is gone or carries a manual summary"
    return True, None


def job_view(cursor: Cursor, job: dict[str, Any]) -> dict[str, Any]:
    """Return a job with the counts that say how far along it is."""
    return {**job, "progress": jobs.job_progress(cursor, int(job["id"]))}


api = APIRouter(dependencies=[Depends(require_token)])


@api.get("/projects")
def get_projects() -> dict[str, Any]:
    """List what can be summarized, and what needs re-indexing first."""
    with transaction() as cursor:
        cursor.execute(
            """
            SELECT p.name, p.root_path, p.indexed_at,
                   COUNT(n.id) FILTER (WHERE n.type = 'file'),
                   COUNT(n.id) FILTER (
                       WHERE n.type = 'file'
                       AND COALESCE(n.metadata ->> 'summary_source', 'auto')
                           = 'auto'
                   )
              FROM projects AS p
              LEFT JOIN graph_nodes AS n ON n.project = p.name
             GROUP BY p.name, p.root_path, p.indexed_at
             ORDER BY p.name;
            """
        )
        rows = cursor.fetchall()
        running = {}
        project_sources = {}
        for name, *_ in rows:
            job = jobs.running_job(cursor, name)
            running[name] = int(job["id"]) if job else None
            project_sources[name] = list_sources(cursor, str(name))
    projects = []
    for name, root_path, indexed_at, files, pending in rows:
        listed = project_sources[name]
        projects.append(
            {
                "name": name,
                "root_path": root_path,
                "sources": [
                    {"alias": alias, "root_path": path} for alias, path in listed
                ],
                "indexed_at": indexed_at,
                "files": int(files),
                # Every directory, or the project is not readable: indexing it
                # while one is missing prunes every node that one produced.
                "mounted": bool(listed)
                and all(
                    os.path.isdir(source_mount(str(name), alias)) for alias, _ in listed
                ),
                "without_llm_summary": int(pending),
                "running_job": running[name],
            }
        )
    return {"projects": projects}


class ProjectRequest(BaseModel):
    """A project to register, with or without a directory to read."""

    name: str = Field(
        default="",
        description="what the project is addressed by; derived from root_path if unset",
    )
    root_path: str = Field(
        default="",
        description="host path of its tree; empty registers a project reading nothing",
    )
    alias: str = Field(
        default="",
        description="what that directory is called inside the project",
    )
    project_type: str = Field(
        default="", description="codebase, docs or config; the default is codebase"
    )


@api.post("/projects", status_code=201)
def post_project(request: ProjectRequest) -> dict[str, Any]:
    """Register a project, so it can be mounted and then indexed.

    Nothing is mounted or indexed by this. The override is a file on the host
    and the services hold the mounts they started with, so the row comes first
    and `make mounts` finishes the job - which is what the reply says.

    A project registered with no directory is the monorepo case: the row, the
    address and the agent files exist, and the slices arrive one at a time
    through `POST /projects/{project}/sources`.
    """
    root_path = request.root_path.strip().rstrip("/")
    project_type = request.project_type.strip() or None
    if project_type is not None and project_type not in KNOWN_PROJECT_TYPES:
        LOG.warning(
            "type=%s is not one of %s; storing it anyway",
            project_type,
            ", ".join(sorted(KNOWN_PROJECT_TYPES)),
        )
    try:
        # Inside the guard: `project_name` refuses a reserved or underivable
        # name, and that is the caller's mistake rather than this service's.
        name = project_name(request.name.strip(), root_path)
        with transaction() as cursor:
            register_project(
                cursor,
                name,
                # A project reading nothing still needs a root_path: the column
                # is NOT NULL UNIQUE, and where it was registered from is the
                # honest answer until its first directory arrives.
                root_path or registered_root(name),
                project_type,
                source_alias(request.alias.strip(), root_path) if request.alias else "",
                with_source=bool(root_path),
            )
            view = source_view(cursor, name)
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["mounts"] = "run `make mounts` on the host, then index the project"
    return view


@api.get("/projects/{project}/settings")
def get_settings(project: str) -> dict[str, Any]:
    """Say where each source of a project would read its selection from now.

    `project_sources.keep_source` records what the last run actually used;
    this is the live answer, and the two differ exactly when the selection was
    changed since. Only the origin is reported - the documents themselves are
    in `project_settings`, which the dashboard reads directly.
    """
    with transaction() as cursor:
        listed = list_sources(cursor, project)
        resolved = []
        for alias, _ in listed:
            mount = source_mount(project, alias)
            if not os.path.isdir(mount):
                resolved.append({"alias": alias, "mounted": False})
                continue
            selection = resolve(cursor, project, alias, mount)
            resolved.append(
                {
                    "alias": alias,
                    "mounted": True,
                    "keep_source": selection.keep_origin,
                    "ignore_source": selection.ignore_origin,
                    "keep_file": present(mount, KEEP_FILES) is not None,
                    "ignore_file": present(mount, IGNORE_FILES) is not None,
                }
            )
    return {"project": project, "sources": resolved}


def mode_origin(settled: schedule.ProjectSchedule) -> str:
    """Name the level a reader would edit to change the folded mode.

    The fold takes the most eager directory, so the level that decided it is
    the first one still saying what the project as a whole does.
    """
    for one in settled.per_alias.values():
        if one.mode == settled.mode:
            return one.origins[schedule.MODE]
    return "default"


def schedule_summary(settled: schedule.ProjectSchedule, project: str) -> dict[str, Any]:
    """Return the folded schedule of one project, as a listing shows it."""
    return {
        "project": project,
        "mode": settled.mode,
        "interval_minutes": settled.interval_minutes,
        "debounce_minutes": settled.debounce_minutes,
        "watched": len(settled.watched),
        "origin": mode_origin(settled),
    }


@api.get("/schedules")
def get_schedules() -> dict[str, Any]:
    """Fold every project at once, for a listing that shows a row each.

    The dashboard would otherwise ask per project, and the fold is the one
    thing it cannot work out for itself.
    """
    with transaction() as cursor:
        aliases: dict[str, list[str]] = defaultdict(list)
        for project, alias, _ in list_all_sources(cursor):
            aliases[project].append(alias)
        settled = {
            project: schedule.for_project(cursor, project, names)
            for project, names in aliases.items()
        }
    return {
        "schedules": [
            schedule_summary(one, project) for project, one in sorted(settled.items())
        ],
        "scheduler": SCHEDULER_ENABLED,
    }


@api.get("/projects/{project}/schedule")
def get_schedule(project: str) -> dict[str, Any]:
    """Say when this project indexes itself, and where that was decided.

    The directories of a project fold into one run, so the fold is answered
    here rather than repeated in the dashboard: a rule with two
    implementations is a rule that will eventually disagree with itself.
    """
    with transaction() as cursor:
        aliases = [alias for alias, _ in list_sources(cursor, project)]
        settled = schedule.for_project(cursor, project, aliases)
        last = indexjobs.last_run(cursor, project)
    return {
        **schedule_summary(settled, project),
        "watched": list(settled.watched),
        "levels": [
            {
                "alias": alias,
                "mode": one.mode,
                "interval_minutes": one.interval_minutes,
                "debounce_minutes": one.debounce_minutes,
                "origins": one.origins,
            }
            for alias, one in settled.per_alias.items()
        ],
        "last_run": last,
        "next_run": schedule.next_due(settled, last),
        "scheduler": SCHEDULER_ENABLED,
    }


class ScanRequest(BaseModel):
    """Which directory of a project to propose a selection for."""

    alias: str = Field(default="", description="the source to scan")


@api.post("/projects/{project}/scan")
def post_scan(project: str, request: ScanRequest) -> dict[str, Any]:
    """Propose a selection for one directory, and say what it would select.

    The same scan `make install` runs before a project exists, pointed at a
    mount instead: the file types actually present decide the proposal, read
    from the parser tables rather than restated. Nothing is stored - the
    caller accepts it by saving it.
    """
    alias = request.alias.strip()
    mount = source_mount(project, alias)
    if not os.path.isdir(mount):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{project} does not read {mount}; the override has to be "
                "rewritten and this service recreated before it can be scanned"
            ),
        )
    inventory = bootstrap.collect(mount)
    keep_lines = bootstrap.keep_document(inventory)
    ignore_lines = bootstrap.ignore_document(inventory)
    report = bootstrap.verify(
        mount,
        inventory,
        to_spec(keep_lines),
        to_spec(ignore_lines),
    )
    return {
        "project": project,
        "alias": alias,
        "ctxkeep": "\n".join(keep_lines) + "\n",
        "ctxignore": "\n".join(ignore_lines) + "\n",
        "report": "\n".join(report),
    }


class AbsorbRequest(BaseModel):
    """Another project to fold into this one, as directories of it."""

    project: str = Field(
        min_length=1, description="the project being moved in, which is dropped"
    )
    alias: str = Field(
        default="",
        description=(
            "what its tree is called inside this project; its own name when "
            "unset, and refused when it already reads named directories"
        ),
    )


class MoveRequest(BaseModel):
    """Where one directory of a project should be read instead."""

    project: str = Field(min_length=1, description="the project it moves to")
    alias: str = Field(
        default="",
        description=(
            "what it is called there; the alias it has now when unset, and "
            "empty only for a project that reads nothing else, which then "
            "reads this tree whole"
        ),
    )


class RenameRequest(BaseModel):
    """The name a project takes instead of the one it has."""

    project: str = Field(min_length=1, description="the name it is given")


class DetachRequest(BaseModel):
    """A directory to take out of a project, as a project of its own."""

    project: str = Field(
        default="",
        description="name for the new project; derived from the path when unset",
    )
    project_type: str = Field(
        default="", description="codebase, docs or config; the default is codebase"
    )


class SourceRequest(BaseModel):
    """One more directory for a project to read."""

    root_path: str = Field(min_length=1, description="host path of the directory")
    alias: str = Field(
        default="",
        description="what it is called inside the project; derived when unset",
    )


def source_key(alias: str) -> str:
    """Read an alias out of a URL path segment.

    The unnamed source is a project mounted whole, and the empty string is not
    a path segment, so `-` stands for it - the sentinel the dashboard already
    uses for the same reason.
    """
    return "" if alias == "-" else alias


def source_view(cursor: Cursor, project: str) -> dict[str, Any]:
    """Return what a project reads, and whether the host has it mounted."""
    listed = list_sources(cursor, project)
    return {
        "project": project,
        "sources": [
            {
                "alias": alias,
                "root_path": path,
                "mounted": os.path.isdir(source_mount(project, alias)),
            }
            for alias, path in listed
        ],
    }


@api.get("/projects/{project}/sources")
def get_sources(project: str) -> dict[str, Any]:
    """List the directories one project is built from."""
    with transaction() as cursor:
        return source_view(cursor, project)


@api.post("/projects/{project}/sources", status_code=201)
def post_source(project: str, request: SourceRequest) -> dict[str, Any]:
    """Add a directory to a project.

    Nothing is mounted by this: the override is a file on the host, and the
    services read the mounts they were started with. `make mounts` writes it
    and recreates them, which is what the reply says.
    """
    root_path = request.root_path.strip().rstrip("/")
    # Always named, even when the caller passed no alias: this endpoint adds a
    # directory to a project, and the unnamed source - the project mounted
    # whole - is settled when the project is onboarded, on the host.
    alias = source_alias(request.alias.strip(), root_path)
    try:
        with transaction() as cursor:
            add_source(cursor, project, alias, root_path)
            view = source_view(cursor, project)
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["mounts"] = "run `make mounts` on the host, then index the project"
    return view


@api.delete("/projects/{project}/sources/{alias}")
def delete_source(project: str, alias: str) -> dict[str, Any]:
    """Stop a project reading one directory."""
    try:
        with transaction() as cursor:
            drop_source(cursor, project, alias)
            view = source_view(cursor, project)
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["mounts"] = "run `make mounts` on the host, then index the project"
    return view


class MemberRequest(BaseModel):
    """One project for an organization to hold."""

    project: str = Field(min_length=1, description="the project it takes in")


def member_view(cursor: Cursor, organization: str) -> dict[str, Any]:
    """Return what an organization holds, and what each member reads.

    `owned` says which of the two memberships each one is: a project moved in,
    which is listed here rather than with the others, or one added, which is a
    reference beside its own listing.
    """
    owned = set(list_owned(cursor, organization))
    names = list_members(cursor, organization)
    described = describe_projects(cursor, names)
    return {
        "project": organization,
        "members": [
            {
                "project": name,
                "owned": name in owned,
                "description": described.get(name),
                "sources": source_view(cursor, name)["sources"],
            }
            for name in names
        ],
    }


@api.get("/projects/{project}/members")
def get_members(project: str) -> dict[str, Any]:
    """List the projects an organization holds."""
    with transaction() as cursor:
        return member_view(cursor, project)


@api.get("/projects/{project}/organizations")
def get_memberships(project: str) -> dict[str, Any]:
    """List the organizations a project is part of.

    A project belongs to as many as it is relevant to, and it refuses to be
    dropped or dissolved while any of them lists it.
    """
    with transaction() as cursor:
        return {"project": project, "organizations": list_memberships(cursor, project)}


class MembershipsRequest(BaseModel):
    """The organizations a project is to be part of, and no others."""

    organizations: list[str] = Field(
        default_factory=list,
        description="every organization holding it after this; the rest let go",
    )


@api.put("/projects/{project}/organizations")
def put_memberships(project: str, request: MembershipsRequest) -> dict[str, Any]:
    """Move a project into these organizations, out of the ones not named.

    Adding a project to an organization is one more row; this is where it
    belongs settled outright, which is what moving it into one means. Neither
    touches its tree, its mount, its node ids or its graph.
    """
    names = [one.strip() for one in request.organizations if one.strip()]
    try:
        with transaction() as cursor:
            set_memberships(cursor, project, names)
            return {
                "project": project,
                "organizations": list_memberships(cursor, project),
            }
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@api.post("/projects/{project}/members", status_code=201)
def post_member(project: str, request: MemberRequest) -> dict[str, Any]:
    """Add one project to an organization.

    Nothing is mounted, moved or copied by this, so - unlike every other route
    here - there is no `make mounts` to run afterwards. The member keeps its
    own tree, its own address and its own graph, indexed once however many
    organizations hold it.
    """
    try:
        with transaction() as cursor:
            add_member(cursor, project, request.project.strip())
            return member_view(cursor, project)
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@api.delete("/projects/{project}/members/{member}")
def delete_member(project: str, member: str) -> dict[str, Any]:
    """Take one project out of an organization, leaving the project itself."""
    try:
        with transaction() as cursor:
            drop_member(cursor, project, member)
            return member_view(cursor, project)
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@api.post("/projects/{project}/sources/{alias}/move")
def post_move(project: str, alias: str, request: MoveRequest) -> dict[str, Any]:
    """Move one directory of a project into another project.

    Nothing is mounted or unmounted by this, as with every other route here:
    the override is a file on the host and both services hold the mounts they
    started with. Both graphs change, so both ends are worth re-indexing - the
    directory brings none of its nodes with it.

    Moving a project's only directory is that project moving, so its row is
    dropped and the records written about its name follow the directory. The
    reply says which happened.
    """
    target = request.project.strip()
    try:
        with transaction() as cursor:
            moved = move_source(
                cursor,
                project,
                source_key(alias),
                target,
                request.alias.strip(),
                drop_empty=True,
            )
            view = source_view(cursor, project)
            view["target"] = source_view(cursor, target)
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["moved"] = moved
    view["mounts"] = "run `make mounts` on the host, then index the project"
    return view


@api.post("/projects/{project}/sources/{alias}/detach", status_code=201)
def post_detach(project: str, alias: str, request: DetachRequest) -> dict[str, Any]:
    """Take one directory out of a project and make a project of it.

    The inverse of `/absorb`: the tree is mounted whole under the new name and
    its node ids lose the alias they carried, so the new project is indexed
    before its graph says anything.
    """
    project_type = request.project_type.strip() or None
    if project_type is not None and project_type not in KNOWN_PROJECT_TYPES:
        LOG.warning(
            "type=%s is not one of %s; storing it anyway",
            project_type,
            ", ".join(sorted(KNOWN_PROJECT_TYPES)),
        )
    try:
        with transaction() as cursor:
            moved = detach_source(
                cursor,
                project,
                source_key(alias),
                request.project.strip(),
                project_type,
            )
            view = source_view(cursor, project)
            view["target"] = source_view(cursor, str(moved["project"]))
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["moved"] = moved
    view["mounts"] = "run `make mounts` on the host, then index the project"
    return view


@api.post("/projects/{project}/absorb")
def post_absorb(project: str, request: AbsorbRequest) -> dict[str, Any]:
    """Fold another project into this one, and drop the one that moved.

    The directories move rather than being copied, so nothing is indexed twice,
    and the plans, memories and suggestions written about the old name follow
    it. Every node id gains the alias as its first segment, which only the next
    index run produces - the same contract naming a project's root has.
    """
    try:
        with transaction() as cursor:
            absorbed = absorb_project(
                cursor, project, request.project.strip(), request.alias.strip()
            )
            view = source_view(cursor, project)
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["absorbed"] = absorbed
    view["mounts"] = "run `make mounts` on the host, then index the project"
    return view


@api.post("/projects/{project}/rename")
def post_rename(project: str, request: RenameRequest) -> dict[str, Any]:
    """Give a project another name, keeping everything it has.

    Rows and nothing else: the graph, the directories, the settings and the
    memberships are re-keyed where they stand, and the records written about
    the old name follow it. No tree is re-read and no node id changes, because
    a node id is relative to a directory rather than to the project.

    The two things outside the database do not follow. The mount is a file on
    the host, so `make mounts` writes it and the services are restarted into
    it; and an onboarded codebase points its own `.mcp.json` at
    `/mcp/<old name>`, which is the caller's to change.
    """
    try:
        with transaction() as cursor:
            renamed = rename_project(cursor, project, request.project.strip())
            view = source_view(cursor, str(renamed["project"]))
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["renamed"] = renamed
    view["mounts"] = "run `make mounts` on the host, then restart the services"
    return view


def resolve_target(cursor: Cursor, project: str, root_path: str) -> tuple[str, str]:
    """Settle which project a request means, and where its tree lives.

    A shell alias knows only `$(pwd)`, so the name is derived here rather than
    on the host: `project_name` is the one implementation of that rule, and a
    second copy of it would eventually disagree with the mounts.
    """
    if project:
        cursor.execute("SELECT root_path FROM projects WHERE name = %s;", (project,))
        row = cursor.fetchone()
        if row is None and not root_path:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no project named {project!r}; pass root_path to index a "
                    "tree for the first time"
                ),
            )
        return project, root_path or str(row[0])

    if not root_path:
        raise HTTPException(
            status_code=422, detail="name the project, or pass its root_path"
        )
    # An already indexed tree keeps the name it was given, which may not be
    # the one its last path segment would produce. A directory read under an
    # alias is asked for by name too: only a project mounted whole carries its
    # tree in `projects.root_path`, so a slice is found through its source.
    cursor.execute("SELECT name FROM projects WHERE root_path = %s;", (root_path,))
    row = cursor.fetchone()
    known = str(row[0]) if row else source_owner(cursor, root_path)
    return (known or project_name("", root_path)), root_path


def index_targets(
    cursor: Cursor, project: str, alias: str
) -> list[tuple[str, list[str] | None]]:
    """Say which projects a run asked for covers, and which directories of each.

    A plain project is itself, whole, or the one directory that was named. An
    organization asked for as a whole is a fan-out: it holds projects and it
    may hold directories, and pressing Index on it means every one of them
    that is not turned off. `off` is what a directory or a member says to be
    left out of a run nobody asked for it by name - so it is honoured here,
    and ignored by the buttons that name one thing.
    """
    if alias:
        return [(project, [alias])]
    if stored_type(cursor, project) != ORGANIZATION_PROJECT_TYPE:
        return [(project, None)]

    aliases = [name for name, _ in list_sources(cursor, project)]
    settled = schedule.for_project(cursor, project, aliases).per_alias
    own = [name for name in aliases if settled[name].mode != "off"]
    targets: list[tuple[str, list[str] | None]] = [(project, own)] if own else []
    for member in list_members(cursor, project):
        held = [name for name, _ in list_sources(cursor, member)]
        if schedule.for_project(cursor, member, held).mode != "off":
            targets.append((member, None))
    return targets


def latest(runs: list[dict[str, Any]], field: str) -> datetime | None:
    """Return the newest stamp the runs carry in one field, or None."""
    stamps = [one[field] for one in runs if one[field] is not None]
    return max(stamps) if stamps else None


def fold_runs(project: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce the runs of an organization to the one answer a caller reads.

    Shaped like a run of its own, because that is what asked for it: a caller
    polls one project and is told whether anything under it is still going,
    what failed and how much was written. The rows themselves travel with it,
    so the dashboard can name each one without a second round trip.
    """
    status = "done"
    if any(one["status"] == "running" for one in runs):
        status = "running"
    elif any(one["status"] == "failed" for one in runs):
        status = "failed"
    failed = [one for one in runs if one["status"] == "failed"]
    counts = {
        name: sum(one.get(name) or 0 for one in runs) for name in indexjobs.COUNTS
    }
    return {
        "id": None,
        "project": project,
        "aliases": None,
        "status": status,
        "error": "\n".join(
            f"{one['project']}: {one['error']}" for one in failed if one["error"]
        )
        or None,
        "runs": runs,
        "started_at": latest(runs, "started_at"),
        "finished_at": None if status == "running" else latest(runs, "finished_at"),
        **counts,
    }


@api.post("/index", status_code=202)
def post_index(request: IndexRequest) -> dict[str, Any]:
    """Start indexing a project, and answer before it finishes.

    The work runs on a thread of this process: the trees are mounted here and
    the parsers are in this image, so nothing has to start a container. Poll
    `/index/{id}` for how it went.

    Naming an alias walks that directory alone, prunes only what it produced,
    and needs only its mount - so one slice of a project is re-read while
    another is missing, which a run over the whole project refuses.

    An organization starts one run per project it holds, so what a caller gets
    back is the fold rather than a row. A member already indexing is skipped
    with its reason: it is what was asked for, being done already.
    """
    requested = request.project_type.strip() or None
    alias = request.alias.strip()
    started: list[tuple[dict[str, Any], str, str | None, list[str] | None]] = []
    skipped: list[dict[str, str]] = []
    with transaction() as cursor:
        project, root_path = resolve_target(
            cursor, request.project.strip(), request.root_path.strip()
        )
        targets = index_targets(cursor, project, alias)
        if not targets:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"nothing under {project} asks to be indexed: every "
                    "directory it reads and every project it holds is off"
                ),
            )
        for name, aliases in targets:
            # The type travels with the project it was asked for. A member is
            # indexed as itself: a run started from the organization it
            # belongs to must not relabel it.
            kind = requested if name == project else None
            path = root_path if name == project else resolve_target(cursor, name, "")[1]
            try:
                # The same guard the schedule starts its runs through: whether
                # a project may be indexed right now is one rule, not two.
                view = indexjobs.open_run(cursor, name, kind, request.fresh, aliases)
            except RuntimeError as refused:
                if len(targets) == 1:
                    raise HTTPException(
                        status_code=409, detail=str(refused)
                    ) from refused
                skipped.append({"project": name, "why": str(refused)})
                continue
            started.append((view, path, kind, aliases))

    if not started:
        raise HTTPException(
            status_code=409,
            detail="; ".join(f"{one['project']}: {one['why']}" for one in skipped),
        )
    for view, path, kind, aliases in started:
        indexjobs.run_in_background(
            view["id"], view["project"], path, kind, request.fresh, aliases
        )
    if len(targets) == 1 and not skipped:
        return started[0][0]
    runs = [view for view, _, _, _ in started]
    return {**fold_runs(project, runs), "skipped": skipped}


@api.get("/projects/{project}/index")
def get_project_index(
    project: str,
    alias: str | None = Query(default=None, description="one directory of it"),
) -> dict[str, Any] | None:
    """Say how this project last indexed, folded when it holds other projects.

    A directory is answered by the last run that covered it, which is either a
    run that named it or a run over the whole project. An organization is
    answered by itself and every project it holds at once, because that is
    what its Index button started.
    """
    with transaction() as cursor:
        if stored_type(cursor, project) != ORGANIZATION_PROJECT_TYPE:
            found = indexjobs.recent_jobs(cursor, project, 1, alias)
            return found[0] if found else None
        names = [project, *list_members(cursor, project)]
        runs = [
            found[0]
            for found in (indexjobs.recent_jobs(cursor, name, 1) for name in names)
            if found
        ]
    return fold_runs(project, runs) if runs else None


@api.get("/index")
def get_index_jobs(
    project: str | None = Query(default=None, description="one project, or all"),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """List index runs, newest first."""
    with transaction() as cursor:
        return {"jobs": indexjobs.recent_jobs(cursor, project, limit)}


@api.get("/index/{job_id}")
def get_index_job(job_id: int) -> dict[str, Any]:
    """Show one index run, running or finished."""
    with transaction() as cursor:
        view = indexjobs.job_row(cursor, job_id)
    if view is None:
        raise HTTPException(status_code=404, detail="unknown index job")
    return view


@api.get("/content")
def get_content(
    project: str = Query(description="the project the file belongs to"),
    path: str = Query(description="the file, relative to the project root"),
    limit: int = Query(default=0, ge=0, description="characters, 0 for all"),
) -> dict[str, Any]:
    """Read one file of an indexed tree from its mount.

    The graph names files; this is what serves their text, so that nothing
    else needs a copy of the tree or a column holding one. A file the deny
    list covers is refused whether or not it is on disk.
    """
    content, reason = sources.read(project, path, limit)
    if content is None:
        status = 403 if reason == sources.DENIED else 404
        raise HTTPException(status_code=status, detail=reason)
    return {
        "project": project,
        "path": path,
        "chars": len(content),
        "content": content,
    }


@api.post("/jobs", status_code=201)
def post_job(request: JobRequest) -> dict[str, Any]:
    """Open a job over every file of a project that still needs describing."""
    input_chars = request.input_chars or LLM_INPUT_CHARS
    if not 1 <= input_chars <= LLM_INPUT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"input_chars must be between 1 and {LLM_INPUT_CHARS}",
        )
    lease_seconds = request.lease_seconds or WORKER_LEASE_SECONDS
    if not 30 <= lease_seconds <= 3600:
        raise HTTPException(
            status_code=422, detail="lease_seconds must be between 30 and 3600"
        )

    with transaction() as cursor:
        cursor.execute("SELECT 1 FROM projects WHERE name = %s;", (request.project,))
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="unknown project")
        open_job = jobs.running_job(cursor, request.project)
        if open_job is not None:
            raise HTTPException(
                status_code=409,
                detail=f"job {open_job['id']} is still running for this project",
            )
        job_id = jobs.create_job(
            cursor,
            request.project,
            input_chars,
            request.refresh,
            lease_seconds,
            request.model,
        )
        total = jobs.populate_job(
            cursor, job_id, request.project, request.refresh, request.limit
        )
        jobs.finish_job_if_drained(cursor, job_id)
        view = job_view(cursor, jobs.job_row(cursor, job_id) or {})

    hint = None
    if not os.path.isdir(project_mount(request.project)):
        hint = "the project is not mounted, so no file can be read; run `make mounts`"
    return {**view, "enqueued": total, "hint": hint}


@api.get("/jobs")
def get_jobs(
    project: str | None = None,
    status: str | None = None,
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List jobs, newest first."""
    with transaction() as cursor:
        rows, total = jobs.list_jobs(cursor, project, status, limit, offset)
        items = [job_view(cursor, row) for row in rows]
    return {"jobs": items, "total": total, "limit": limit, "offset": offset}


@api.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    """One job with its progress."""
    with transaction() as cursor:
        row = jobs.job_row(cursor, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job_view(cursor, row)


@api.post("/jobs/{job_id}/cancel")
def post_cancel(job_id: int) -> dict[str, Any]:
    """Stop handing out work. Leases already held are left to expire."""
    with transaction() as cursor:
        if jobs.job_row(cursor, job_id) is None:
            raise HTTPException(status_code=404, detail="unknown job")
        jobs.cancel_job(cursor, job_id)
        return job_view(cursor, jobs.job_row(cursor, job_id) or {})


@api.get("/jobs/{job_id}/files")
def get_job_files(
    job_id: int,
    state: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Page through the files of a job. Their text is only ever leased."""
    with transaction() as cursor:
        if jobs.job_row(cursor, job_id) is None:
            raise HTTPException(status_code=404, detail="unknown job")
        rows, total = jobs.list_job_files(cursor, job_id, state, limit, offset)
    return {"files": rows, "total": total, "limit": limit, "offset": offset}


@api.post("/jobs/{job_id}/lease")
def post_lease(job_id: int, request: LeaseRequest) -> dict[str, Any]:
    """Claim a batch, and be handed the text and the prompt to run on it.

    The batch can come back smaller than asked, or empty: cache hits and files
    the mount does not hold are settled here and never leased.
    """
    token = str(uuid.uuid4())
    batch = min(request.batch, WORKER_MAX_BATCH)
    with transaction() as cursor:
        job = jobs.job_row(cursor, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        if job["status"] != "running":
            raise HTTPException(status_code=409, detail=f"job is {job['status']}")

        project = str(job["project"])
        input_chars = int(job["input_chars"])
        lease_seconds = request.lease_seconds or int(job["lease_seconds"])

        jobs.reclaim_expired(cursor, job_id)
        if not job["refresh"]:
            for _, rel_path, summary in jobs.settle_cached(cursor, job_id, project):
                apply_summary(cursor, project, rel_path, summary)

        claimed = jobs.claim_batch(
            cursor,
            job_id,
            batch,
            token,
            request.worker_id,
            lease_seconds,
            WORKER_MAX_ATTEMPTS,
        )
        texts = (
            jobs.read_task_content(
                cursor, project, [task["task_id"] for task in claimed], input_chars
            )
            if claimed
            else {}
        )

        tasks: list[dict[str, Any]] = []
        empty: list[int] = []
        for task in claimed:
            text = texts.get(int(task["task_id"]), "")
            if not text:
                empty.append(int(task["task_id"]))
                continue
            digest = content_key(text)
            if digest != task["content_hash"]:
                jobs.set_task_hash(cursor, int(task["task_id"]), digest)
            # The digest is only knowable once the file has been read, so the
            # cache is consulted here rather than when the job was created.
            if not job["refresh"]:
                cached = get_cached_summary(cursor, project, digest)
                if cached is not None:
                    apply_summary(cursor, project, str(task["file_path"]), cached)
                    jobs.settle_task(cursor, int(task["task_id"]))
                    continue
            tasks.append(
                {
                    "task_id": int(task["task_id"]),
                    "file_path": task["file_path"],
                    "content_hash": digest,
                    "attempts": int(task["attempts"]),
                    "prompt": f"File: {task['file_path']}\n\n{text}",
                }
            )
        jobs.skip_tasks(cursor, empty, jobs.NO_FILE)
        jobs.finish_job_if_drained(cursor, job_id)
        progress = jobs.job_progress(cursor, job_id)
        status = str((jobs.job_row(cursor, job_id) or {}).get("status", "running"))

    return {
        "lease_token": token,
        "lease_seconds": lease_seconds,
        "job": {
            "id": job_id,
            "project": project,
            "status": status,
            "input_chars": input_chars,
        },
        "system_prompt": SYSTEM_PROMPT,
        "max_tokens": LLM_MAX_TOKENS,
        "tasks": tasks,
        "remaining": progress["pending"],
    }


@api.post("/jobs/{job_id}/heartbeat")
def post_heartbeat(job_id: int, request: LeaseHeld) -> dict[str, Any]:
    """Push back the deadline of the whole batch this token covers."""
    with transaction() as cursor:
        job = jobs.job_row(cursor, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        seconds = request.lease_seconds or int(job["lease_seconds"])
        extended = jobs.extend_lease(cursor, job_id, str(request.lease_token), seconds)
    if not extended:
        raise HTTPException(status_code=409, detail="lease is no longer held")
    return {"extended": extended, "lease_seconds": seconds}


@api.post("/jobs/{job_id}/release")
def post_release(job_id: int, request: LeaseHeld) -> dict[str, Any]:
    """Hand an unfinished batch back at once, rather than waiting it out."""
    with transaction() as cursor:
        if jobs.job_row(cursor, job_id) is None:
            raise HTTPException(status_code=404, detail="unknown job")
        released = jobs.release_lease(cursor, job_id, str(request.lease_token))
    return {"released": released}


@api.post("/tasks/{task_id}/result")
def post_result(task_id: int, request: ResultRequest) -> dict[str, Any]:
    """Take one answer, shape it, and put it on the node if it says anything."""
    if len(request.summary) > WORKER_MAX_REPLY_CHARS:
        raise HTTPException(status_code=413, detail="reply too long")

    with transaction() as cursor:
        task = jobs.lock_leased_task(cursor, task_id, str(request.lease_token))
        if task is None:
            raise HTTPException(
                status_code=409, detail="lease expired or already settled"
            )

        project = str(task["project"])
        rel_path = str(task["file_path"])
        summary = strip_preamble(shape(request.summary), rel_path)
        if not summary:
            state = jobs.fail_task(cursor, task_id, "empty reply", WORKER_MAX_ATTEMPTS)
            return {
                "task_id": task_id,
                "state": state,
                "applied": False,
                "summary": None,
                "reason": "empty reply",
            }

        put_cached_summary(cursor, project, str(task["content_hash"]), summary)
        applied, reason = apply_summary(cursor, project, rel_path, summary)
        jobs.finish_task(cursor, task_id, reason)
        jobs.finish_job_if_drained(cursor, task["job_id"])
        status = str((jobs.job_row(cursor, task["job_id"]) or {}).get("status", ""))

    return {
        "task_id": task_id,
        "state": "done",
        "applied": applied,
        "summary": summary,
        "reason": reason,
        "job_status": status,
    }


@api.post("/tasks/{task_id}/failure")
def post_failure(task_id: int, request: FailureRequest) -> dict[str, Any]:
    """Report that the model could not describe a file."""
    with transaction() as cursor:
        task = jobs.lock_leased_task(cursor, task_id, str(request.lease_token))
        if task is None:
            raise HTTPException(
                status_code=409, detail="lease expired or already settled"
            )
        state = jobs.fail_task(
            cursor, task_id, request.error[:500] or "worker failed", WORKER_MAX_ATTEMPTS
        )
        jobs.finish_job_if_drained(cursor, task["job_id"])
    return {"task_id": task_id, "state": state, "attempts": int(task["attempts"])}


def create_app() -> FastAPI:
    """Build the application, refusing to serve without a token."""
    if len(WORKER_API_TOKEN) < WORKER_API_TOKEN_MIN:
        raise RuntimeError(
            "WORKER_API_TOKEN is unset or shorter than "
            f"{WORKER_API_TOKEN_MIN} characters. This service is published to "
            "the network and serves the text of your files; generate one with "
            "`openssl rand -hex 24`."
        )
    docs = "/docs" if WORKER_API_DOCS else None
    app = FastAPI(
        title="enggraph worker API",
        docs_url=docs,
        redoc_url=None,
        openapi_url="/openapi.json" if WORKER_API_DOCS else None,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness, and whether the database is answering."""
        try:
            with transaction() as cursor:
                cursor.execute("SELECT 1;")
        except psycopg2.Error:
            raise HTTPException(
                status_code=503, detail="database unreachable"
            ) from None
        return {"status": "ok", "database": "ok"}

    app.include_router(api)
    if SCHEDULER_ENABLED:
        # Imported here so the package stays importable where `watchfiles` is
        # not installed: only the service that holds the mounts schedules runs.
        from enggraph.scheduler import Scheduler

        Scheduler().start()
    return app


def main() -> None:
    """Run the API."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(create_app(), host="0.0.0.0", port=WORKER_API_PORT)


if __name__ == "__main__":
    main()
