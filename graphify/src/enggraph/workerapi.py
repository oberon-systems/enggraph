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

import faulthandler
import logging
import os
import secrets
import signal
import threading
import uuid
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

from enggraph import bootstrap, embedjobs, features, indexjobs, jobs, schedule, sources
from enggraph.config import (
    EMBED_CHUNK_CHARS,
    EMBED_LOOP_ENABLED,
    EMBED_MODEL,
    FEATURE_EMBEDDING,
    FEATURE_INDEXING,
    FEATURE_SUMMARIZE,
    IGNORE_FILES,
    KEEP_FILES,
    KNOWN_PROJECT_TYPES,
    LLM_INPUT_CHARS,
    LLM_MAX_TOKENS,
    ORGANIZATION_PROJECT_TYPE,
    PROBE_TIMEOUTS,
    SCHEDULER_ENABLED,
    SUMMARIZE_LOOP_ENABLED,
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
from enggraph.embedder import Embedder, EmbedError, candidates, primary
from enggraph.identifiers import project_mount, project_name
from enggraph.llamachat import Chat, ChatError
from enggraph.llamachat import candidates as chat_candidates
from enggraph.selection import resolve
from enggraph.storage import (
    SKIP_EMBED,
    SKIP_SUMMARIZE,
    add_member,
    drop_member,
    embedding_coverage,
    get_cached_summary,
    get_db_url,
    list_members,
    list_memberships,
    list_mountable_projects,
    list_owned,
    list_skipped,
    mark_skip,
    project_rows,
    put_cached_summary,
    register_project,
    registered_root,
    rename_project,
    save_llm_summary,
    set_memberships,
    stored_type,
    summary_coverage,
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
# Built under a lock. Without one, two requests arriving together each saw no
# pool and each built one: the second won the global, and returning a
# connection borrowed from the first raised "trying to put unkeyed
# connection" - a 500 on a read that had already done its work.
_pool_lock = threading.Lock()
# Whether the last query was embedded by the local fallback; logged on change only.
_query_fell_back = False


def pool() -> ThreadedConnectionPool:
    """Return the connection pool, opening it on first use.

    A pool rather than a connection per request: this is a long-lived service,
    unlike every other user of `storage`, which is a job that runs once.
    """
    global _pool
    with _pool_lock:
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


class JobRequest(BaseModel):
    """What to summarize, and how much of each file to show the model."""

    project: str
    refresh: bool = False
    input_chars: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    lease_seconds: int = Field(default=0, ge=0)
    model: str | None = None


class EmbedRequest(BaseModel):
    """One string to embed, which is what a search query is."""

    text: str = Field(min_length=1, max_length=8000)
    project: str = Field(
        default="",
        description="whose server URL to use; the global one when unset",
    )


class ProbeRequest(BaseModel):
    """One address to try, so the dashboard can test a URL before storing it.

    The key travels with it and is never stored by this call: a token typed
    into the form has to be testable before it is saved, which is the whole
    point of testing before saving.
    """

    url: str = Field(default="", max_length=500)
    key: str = Field(default="", max_length=500)


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
        # The same text gets the same answer: owed again, it would come back
        # from the cache for ever, so it is listed as failed instead.
        mark_skip(cursor, project, rel_path, SKIP_SUMMARIZE, NOT_USEFUL)
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
        for name, *_ in rows:
            job = jobs.running_job(cursor, name)
            running[name] = int(job["id"]) if job else None
    projects = []
    for name, root_path, indexed_at, files, pending in rows:
        projects.append(
            {
                "name": name,
                "root_path": root_path,
                "indexed_at": indexed_at,
                "files": int(files),
                "mounted": os.path.isdir(project_mount(str(name))),
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
    project_type: str = Field(
        default="", description="codebase, docs or config; the default is codebase"
    )


@api.post("/projects", status_code=201)
def post_project(request: ProjectRequest) -> dict[str, Any]:
    """Register a project, so it can be mounted and then indexed.

    Nothing is mounted or indexed by this. The override is a file on the host
    and the services hold the mounts they started with, so the row comes first
    and `make mounts` finishes the job - which is what the reply says.

    A project registered with no directory is an organization, or one waiting
    for the tree it will read: the row, the address and the agent files exist
    before anything is mounted.
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
                # honest answer until it is given a tree.
                root_path or registered_root(name),
                project_type,
            )
            view = project_view(cursor, name)
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["mounts"] = "run `make mounts` on the host, then index the project"
    return view


@api.get("/projects/{project}/settings")
def get_settings(project: str) -> dict[str, Any]:
    """Say where a project would read its selection from now.

    `projects.keep_source` records what the last run actually used; this is
    the live answer, and the two differ exactly when the selection was changed
    since. Only the origin is reported - the documents themselves are in
    `project_settings`, which the dashboard reads directly.
    """
    mount = project_mount(project)
    if not os.path.isdir(mount):
        return {"project": project, "mounted": False}
    with transaction() as cursor:
        selection = resolve(cursor, project, mount)
    return {
        "project": project,
        "mounted": True,
        "keep_source": selection.keep_origin,
        "ignore_source": selection.ignore_origin,
        "keep_file": present(mount, KEEP_FILES) is not None,
        "ignore_file": present(mount, IGNORE_FILES) is not None,
    }


def schedule_summary(settled: schedule.Schedule, project: str) -> dict[str, Any]:
    """Return the schedule of one project, as a listing shows it."""
    return {
        "project": project,
        "mode": settled.mode,
        "interval_minutes": settled.interval_minutes,
        "debounce_minutes": settled.debounce_minutes,
        "watched": settled.watched,
        "origin": settled.origins[schedule.MODE],
    }


@api.get("/schedules")
def get_schedules() -> dict[str, Any]:
    """Resolve every project at once, for a listing that shows a row each.

    The dashboard would otherwise ask per project, and which level answered is
    the one thing it cannot work out for itself.
    """
    with transaction() as cursor:
        settled = {
            project: schedule.resolve(cursor, project)
            for project, _ in list_mountable_projects(cursor)
        }
    return {
        "schedules": [
            schedule_summary(one, project) for project, one in sorted(settled.items())
        ],
        "scheduler": SCHEDULER_ENABLED,
    }


def key_summary(settled: features.Feature) -> dict[str, Any]:
    """Say whether a token is stored and when it is due, never what it is.

    The dashboard has no authentication of its own, so a key that travelled
    back to a browser would be readable by anyone who can open the page. What
    it needs is three facts, and none of them is the secret.
    """
    due = settled.key_due()
    return {
        "key_set": bool(settled.server_key),
        "key_saved_at": settled.key_saved_at or None,
        "key_due": due.isoformat() if due else None,
        "key_expired": settled.key_expired,
    }


def embedding_summary(cursor: Cursor, project: str) -> dict[str, Any]:
    """Return what one project's vectors amount to, as a listing shows it."""
    settled = features.resolve(cursor, project, FEATURE_EMBEDDING)
    coverage = embedding_coverage(cursor, project)
    return {
        "project": project,
        "allowed": settled.allowed,
        "enabled": settled.enabled,
        "gated": settled.gated,
        "origin": settled.origins[features.ENABLED],
        "server_url": settled.server_url,
        "urls": candidates(settled.server_url),
        "batch": settled.batch,
        "tick_seconds": settled.tick_seconds,
        "budget_seconds": settled.budget_seconds,
        "queue": embedjobs.queue_depth(cursor, project),
        **key_summary(settled),
        **coverage,
    }


def environment_url(name: str) -> str:
    """Return the server a feature dials when no level stores one."""
    if name == FEATURE_EMBEDDING:
        return primary("")
    if name == FEATURE_SUMMARIZE:
        return next(iter(chat_candidates("")), "")
    return ""


def inherited_summary(cursor: Cursor, project: str, name: str) -> dict[str, Any]:
    """Return what a level would get by saying nothing, for the placeholders."""
    parent = features.resolve(cursor, project, name, inherited=True)
    origins: dict[str, str] = dict(parent.origins)
    url = parent.server_url
    if not url:
        url = environment_url(name)
        origins[features.SERVER_URL] = "environment" if url else "default"
    return {
        "enabled": parent.enabled,
        "server_url": url,
        "batch": parent.batch,
        "tick_seconds": parent.tick_seconds,
        "budget_seconds": parent.budget_seconds,
        "chunk_chars": parent.chunk_chars,
        "chunk_overlap": parent.chunk_overlap,
        "origins": origins,
    }


def feature_summary(cursor: Cursor, project: str, name: str) -> dict[str, Any]:
    """One feature of one project, settled, with where each field came from."""
    settled = features.resolve(cursor, project, name)
    return {
        "inherited": inherited_summary(cursor, project, name),
        "feature": name,
        "allowed": settled.allowed,
        "enabled": settled.enabled,
        "gated": settled.gated,
        "server_url": settled.server_url,
        "batch": settled.batch,
        "tick_seconds": settled.tick_seconds,
        "budget_seconds": settled.budget_seconds,
        "chunk_chars": settled.chunk_chars,
        "chunk_overlap": settled.chunk_overlap,
        "origins": settled.origins,
        **key_summary(settled),
    }


@api.get("/projects/{project}/features")
def get_features(project: str) -> dict[str, Any]:
    """Settle every switchable feature of one project.

    Asked of this service rather than worked out in the dashboard, for the
    reason the schedule is: the resolution is one rule, and a second
    implementation of it in another language would eventually disagree with
    the one that acts on it.
    """
    with transaction() as cursor:
        cursor.execute("SELECT 1 FROM projects WHERE name = %s;", (project,))
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="unknown project")
        settled = {
            name: feature_summary(cursor, project, name)
            for name in (FEATURE_INDEXING, FEATURE_SUMMARIZE, FEATURE_EMBEDDING)
        }
    return {"project": project, "features": settled}


def summary_summary(cursor: Cursor, project: str) -> dict[str, Any]:
    """Return what one project's summaries amount to, as a listing shows it."""
    settled = features.resolve(cursor, project, FEATURE_SUMMARIZE)
    open_job = jobs.running_job(cursor, project)
    return {
        "project": project,
        "allowed": settled.allowed,
        "enabled": settled.enabled,
        "gated": settled.gated,
        "origin": settled.origins[features.ENABLED],
        "server_url": settled.server_url,
        "batch": settled.batch,
        "tick_seconds": settled.tick_seconds,
        "budget_seconds": settled.budget_seconds,
        "chunk_chars": settled.chunk_chars,
        "chunk_overlap": settled.chunk_overlap,
        "pushed": bool(chat_candidates(settled.server_url)),
        "job": None if open_job is None else int(open_job["id"]),
        "queue": jobs.queue_depth(cursor, project),
        **key_summary(settled),
        **summary_coverage(cursor, project),
    }


@api.get("/projects/{project}/failures")
def get_failures(project: str) -> dict[str, Any]:
    """List the files of one project each queue gave up on, and why."""
    with transaction() as cursor:
        summaries = list_skipped(cursor, project, SKIP_SUMMARIZE)
        embeddings = list_skipped(cursor, project, SKIP_EMBED)
    return {
        "project": project,
        "summaries": [{"file_path": path, "error": why} for path, why in summaries],
        "embeddings": [{"file_path": path, "error": why} for path, why in embeddings],
    }


@api.get("/summaries")
def get_summaries() -> dict[str, Any]:
    """Resolve every project's summarizing state at once, for the dashboard.

    The pair of `/embeddings`: the same shape for the other queue, so one page
    can put the two side by side without knowing which is which.
    """
    with transaction() as cursor:
        rows = [
            summary_summary(cursor, project)
            for project, _ in list_mountable_projects(cursor)
        ]
    return {
        "summaries": sorted(rows, key=lambda row: row["project"]),
        "loop": SUMMARIZE_LOOP_ENABLED,
    }


@api.get("/embeddings")
def get_embeddings() -> dict[str, Any]:
    """Resolve every project's embedding state at once, for the dashboard."""
    with transaction() as cursor:
        rows = [
            embedding_summary(cursor, project)
            for project, _ in list_mountable_projects(cursor)
        ]
    return {
        "embeddings": sorted(rows, key=lambda row: row["project"]),
        "model": EMBED_MODEL,
        # What one unit of work is. A file is many chunks, and a queue counted
        # in files looks slower than it is without this.
        "chunk_chars": EMBED_CHUNK_CHARS,
        "loop": EMBED_LOOP_ENABLED,
    }


@api.post("/embed")
def post_embed(request: EmbedRequest) -> dict[str, Any]:
    """Embed one string, which is the only thing the MCP server asks for.

    503 rather than 500 when nothing answers: the caller degrades to its
    lexical half, and the difference between "no server" and "this broke" is
    what tells it which of the two happened.
    """
    global _query_fell_back
    stored, key = "", ""
    if request.project:
        with transaction() as cursor:
            settled = features.resolve(cursor, request.project, FEATURE_EMBEDDING)
        stored, key = settled.server_url, settled.server_key
    embedder = Embedder(stored_url=stored, stored_key=key, for_query=True)
    try:
        vector = embedder.embed_one(request.text)
    except EmbedError as refused:
        raise HTTPException(status_code=503, detail=str(refused)) from None
    fell_back = bool(embedder.primary) and embedder.chosen != embedder.primary
    if fell_back != _query_fell_back:
        _query_fell_back = fell_back
        if fell_back:
            LOG.info(
                "Queries embedded by the local %s: %s is unreachable",
                embedder.chosen,
                embedder.primary,
            )
        else:
            LOG.info("Queries embedded by %s again", embedder.chosen)
    return {
        "model": embedder.model,
        "dimensions": len(vector),
        "server": embedder.chosen,
        "fell_back": fell_back,
        "embedding": vector,
    }


@api.post("/summaries/probe")
def post_summary_probe(request: ProbeRequest) -> dict[str, Any]:
    """Say whether a chat server answers, before it is stored as a setting.

    `/props` rather than a completion: it names the model and the context
    window, costs the server nothing, and a summary asked here would be a
    summary nobody reads.
    """
    chat = Chat(stored_url=request.url, stored_key=request.key)
    try:
        chat.props()
    except ChatError as refused:
        return {"ok": False, "detail": str(refused), "urls": chat.urls}
    return {
        "ok": True,
        "server": chat.chosen,
        "model": chat.model or "model not named",
        "context": chat.n_ctx,
    }


class RetryRequest(BaseModel):
    """Which queue to forgive, and for which project. Empty means all of them."""

    project: str = Field(default="", max_length=64)


@api.post("/embeddings/retry")
def post_embeddings_retry(request: RetryRequest) -> dict[str, Any]:
    """Put failed files back in the embedding queue."""
    with transaction() as cursor:
        count = embedjobs.retry_failed(cursor, request.project or None)
    return {"queued": count}


@api.post("/summaries/retry")
def post_summaries_retry(request: RetryRequest) -> dict[str, Any]:
    """Put failed files back in the summary queue."""
    with transaction() as cursor:
        count = jobs.retry_failed(cursor, request.project or None)
    return {"queued": count}


@api.post("/embeddings/probe")
def post_embed_probe(request: ProbeRequest) -> dict[str, Any]:
    """Say whether an address answers, before it is stored as a setting.

    A URL typed wrong is otherwise found out by a queue that quietly stops,
    which is the failure this endpoint exists to prevent.
    """
    embedder = Embedder(
        stored_url=request.url, stored_key=request.key, timeouts=PROBE_TIMEOUTS
    )
    try:
        vector = embedder.embed_one("ping")
    except EmbedError as refused:
        return {"ok": False, "detail": str(refused), "urls": embedder.urls}
    # Which address answered, and what the ones before it said. Without this
    # a URL that refuses the embeddings route reads as working, because the
    # next address in the list quietly answered instead.
    skipped = [
        note for url, note in embedder.refusals.items() if url != embedder.chosen
    ]
    return {
        "ok": True,
        "server": embedder.chosen,
        "model": embedder.model,
        "dimensions": len(vector),
        "fell_back": bool(skipped) and embedder.chosen != request.url.rstrip("/"),
        "skipped": skipped,
    }


@api.get("/projects/{project}/schedule")
def get_schedule(project: str) -> dict[str, Any]:
    """Say when this project indexes itself, and where that was decided.

    Every field is resolved on its own, so `origins` names the level each of
    them came from rather than the level the row as a whole was read at.
    """
    with transaction() as cursor:
        settled = schedule.resolve(cursor, project)
        last = indexjobs.last_run(cursor, project)
    return {
        **schedule_summary(settled, project),
        "origins": settled.origins,
        "last_run": last,
        "next_run": schedule.next_due(settled, last),
        "scheduler": SCHEDULER_ENABLED,
    }


@api.post("/projects/{project}/scan")
def post_scan(project: str) -> dict[str, Any]:
    """Propose a selection for a project, and say what it would select.

    The same scan `make install` runs before a project exists, pointed at a
    mount instead: the file types actually present decide the proposal, read
    from the parser tables rather than restated. Nothing is stored - the
    caller accepts it by saving it.
    """
    mount = project_mount(project)
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
        "ctxkeep": "\n".join(keep_lines) + "\n",
        "ctxignore": "\n".join(ignore_lines) + "\n",
        "report": "\n".join(report),
    }


class RenameRequest(BaseModel):
    """The name a project takes instead of the one it has."""

    project: str = Field(min_length=1, description="the name it is given")


def project_view(cursor: Cursor, project: str) -> dict[str, Any]:
    """Return what a project reads, and whether the host has it mounted."""
    stored = project_rows(cursor, [project]).get(project, {})
    return {
        "project": project,
        "root_path": stored.get("root_path"),
        "mounted": os.path.isdir(project_mount(project)),
    }


class MemberRequest(BaseModel):
    """One project for an organization to hold."""

    project: str = Field(min_length=1, description="the project it takes in")


def member_view(cursor: Cursor, organization: str) -> dict[str, Any]:
    """Return what an organization holds, and how each member is kept.

    `owned` says which of the two memberships each one is: a project moved in,
    which is listed here rather than with the others, or one added, which is a
    reference beside its own listing.
    """
    owned = set(list_owned(cursor, organization))
    names = list_members(cursor, organization)
    stored = project_rows(cursor, names)
    return {
        "project": organization,
        "members": [
            {
                "project": name,
                "owned": name in owned,
                "description": stored.get(name, {}).get("description"),
                "root_path": stored.get(name, {}).get("root_path"),
                "mounted": os.path.isdir(project_mount(name)),
                "indexed_at": stored.get(name, {}).get("indexed_at"),
                "stale_seconds": stored.get(name, {}).get("stale_seconds"),
                # What this member does on its own, resolved through the
                # organization: a row that says `off` is one nothing will
                # reindex, and the level is what a reader would go and edit.
                "schedule": schedule_summary(schedule.resolve(cursor, name), name),
                # And how much of it is searchable by meaning, which is the
                # same question asked of the vectors.
                "embedding": embedding_summary(cursor, name),
                "summary": summary_summary(cursor, name),
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


@api.post("/projects/{project}/rename")
def post_rename(project: str, request: RenameRequest) -> dict[str, Any]:
    """Give a project another name, keeping everything it has.

    Rows and nothing else: the graph, the settings and the memberships are
    re-keyed where they stand, and the records written about the old name
    follow it. No tree is re-read and no node id changes, because a node id is
    relative to the tree rather than to the project.

    The two things outside the database do not follow. The mount is a file on
    the host, so `make mounts` writes it and the services are restarted into
    it; and an onboarded codebase points its own `.mcp.json` at
    `/mcp/<old name>`, which is the caller's to change.
    """
    try:
        with transaction() as cursor:
            renamed = rename_project(cursor, project, request.project.strip())
            view = project_view(cursor, str(renamed["project"]))
    except (RuntimeError, psycopg2.Error) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    view["renamed"] = renamed
    view["mounts"] = "run `make mounts` on the host, then restart the services"
    return view


def resolve_target(cursor: Cursor, project: str, root_path: str) -> tuple[str, str]:
    """Settle which project a request means, and where its tree lives.

    A caller knowing only `$(pwd)` is answered here rather than on the host:
    `project_name` is the one implementation of that rule, and a second copy
    of it would eventually disagree with the mounts.
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
    # the one its last path segment would produce.
    cursor.execute("SELECT name FROM projects WHERE root_path = %s;", (root_path,))
    row = cursor.fetchone()
    return (str(row[0]) if row else project_name("", root_path)), root_path


def index_targets(cursor: Cursor, project: str) -> list[str]:
    """Say which projects a run asked for covers.

    A plain project is itself. An organization is a fan-out: it reads no tree
    of its own, so pressing Index on it means every project it holds that is
    not turned off. `off` is what a member says to be left out of a run nobody
    asked for it by name - so it is honoured here, and ignored by the button
    that names one project.
    """
    if stored_type(cursor, project) != ORGANIZATION_PROJECT_TYPE:
        return [project]
    return [
        member
        for member in list_members(cursor, project)
        if schedule.resolve(cursor, member).mode != "off"
    ]


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

    An organization starts one run per project it holds, so what a caller gets
    back is the fold rather than a row. A member already indexing is skipped
    with its reason: it is what was asked for, being done already.
    """
    requested = request.project_type.strip() or None
    started: list[tuple[dict[str, Any], str, str | None]] = []
    skipped: list[dict[str, str]] = []
    with transaction() as cursor:
        project, root_path = resolve_target(
            cursor, request.project.strip(), request.root_path.strip()
        )
        targets = index_targets(cursor, project)
        if not targets:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"nothing under {project} asks to be indexed: every "
                    "project it holds is off"
                ),
            )
        for name in targets:
            # The type travels with the project it was asked for. A member is
            # indexed as itself: a run started from the organization it
            # belongs to must not relabel it.
            kind = requested if name == project else None
            path = root_path if name == project else resolve_target(cursor, name, "")[1]
            try:
                # The same guard the schedule starts its runs through: whether
                # a project may be indexed right now is one rule, not two.
                view = indexjobs.open_run(cursor, name, kind, request.fresh)
            except RuntimeError as refused:
                if len(targets) == 1:
                    raise HTTPException(
                        status_code=409, detail=str(refused)
                    ) from refused
                skipped.append({"project": name, "why": str(refused)})
                continue
            started.append((view, path, kind))

    if not started:
        raise HTTPException(
            status_code=409,
            detail="; ".join(f"{one['project']}: {one['why']}" for one in skipped),
        )
    for view, path, kind in started:
        indexjobs.run_in_background(
            view["id"], view["project"], path, kind, request.fresh
        )
    if len(targets) == 1 and not skipped:
        return started[0][0]
    runs = [view for view, _, _ in started]
    return {**fold_runs(project, runs), "skipped": skipped}


@api.get("/projects/{project}/index")
def get_project_index(project: str) -> dict[str, Any] | None:
    """Say how this project last indexed, folded when it holds other projects.

    An organization is answered by every project it holds at once, because
    that is what its Index button started.
    """
    with transaction() as cursor:
        if stored_type(cursor, project) != ORGANIZATION_PROJECT_TYPE:
            found = indexjobs.recent_jobs(cursor, project, 1)
            return found[0] if found else None
        names = list_members(cursor, project)
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
        settled = features.resolve(cursor, request.project, FEATURE_SUMMARIZE)
        if not settled.enabled:
            where = "globally" if settled.gated else f"for {request.project}"
            raise HTTPException(
                status_code=409,
                detail=f"summarizing is switched off {where}; "
                "turn it on from the dashboard settings",
            )
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
        for task in claimed:
            text, reason = texts.get(int(task["task_id"]), ("", jobs.NO_FILE))
            if not text:
                jobs.fail_and_mark(
                    cursor,
                    int(task["task_id"]),
                    project,
                    str(task["file_path"]),
                    reason,
                    WORKER_MAX_ATTEMPTS,
                )
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
            state = jobs.fail_and_mark(
                cursor, task_id, project, rel_path, "empty reply", WORKER_MAX_ATTEMPTS
            )
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
        state = jobs.fail_and_mark(
            cursor,
            task_id,
            str(task["project"]),
            str(task["file_path"]),
            request.error[:500] or "worker failed",
            WORKER_MAX_ATTEMPTS,
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
    if EMBED_LOOP_ENABLED:
        # Same reason as above: the queue reads files off the mounts, so it
        # runs in the one service that holds them and nowhere else.
        from enggraph.embedloop import EmbedLoop

        EmbedLoop().start()
    if SUMMARIZE_LOOP_ENABLED:
        # The third way the summary queue is drained, beside `make summarize`
        # and a worker claiming leases. It idles unless a server URL is set.
        from enggraph.summarizeloop import SummarizeLoop

        SummarizeLoop().start()
    return app


def main() -> None:
    """Run the API."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # `docker compose kill -s USR1 worker-api` writes every thread's stack to
    # the log: what a queue that stands still is actually waiting on.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    uvicorn.run(create_app(), host="0.0.0.0", port=WORKER_API_PORT)


if __name__ == "__main__":
    main()
