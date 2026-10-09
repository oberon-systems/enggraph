"""The pages: the same answers the API gives, laid out for a reader."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio.from_thread
from fastapi import APIRouter, Request
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse

from enggraph.core import db, jsjson
from enggraph.web import mcpclient, queues, view
from enggraph.web import queries as sql
from enggraph.web.args import (
    DEFAULT_LIMIT,
    HttpError,
    number,
    read_flag,
    read_query,
    whole,
)
from enggraph.web.routes import (
    links,
    memories,
    nodes,
    plans,
    projects,
    prompts,
    records,
    roadmaps,
    settings,
    skills,
    suggestions,
)
from enggraph.web.upstream import UpstreamError, call

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
NAV = (
    ("/", "Ask"),
    ("/projects", "Projects"),
    ("/memories", "Memories"),
    ("/suggestions", "Suggestions"),
    ("/skills", "Skills"),
    ("/queues", "Queues"),
    ("/settings", "Settings"),
)
PROJECT_TABS = ("indexed", "organizations", "system")
SORTABLE = ("nodes", "edges", "files", "plans", "indexed")
# What an agent wrote about the project, drawn inside it: no page of its own.
RECORD_TABS = ("roadmaps", "plans", "prompts")
TABS = (
    "overview",
    *RECORD_TABS,
    "graph",
    "nodes",
    "files",
    "links",
    "settings",
    "skills",
    "failures",
)
ORGANIZATION_TABS = ("overview", *RECORD_TABS, "links", "settings", "skills")
RECORD_PAGES = {
    "_memory": "/memories",
    "_plans": "/plans",
    "_suggestions": "/suggestions",
}
LINK_KINDS = (
    "image",
    "role",
    "npm",
    "composer",
    "pypi",
    "go",
    "cargo",
    "cmake",
    "vcpkg",
    "deploy-role",
    "deploy-module",
    "host",
    "tfmodule",
    "package",
    "bucket",
)
LINK_RELATIONS = (
    "deploys_to",
    "documents",
    "depends_on",
    "calls",
    "generated_from",
    "implements",
)
WAYS = {
    "contains": "is provided as",
    "taken_by": "is taken by",
    "applied_by": "is applied by",
    "uses": "uses",
}
PLAN_STATUSES = ("active", "completed", "archived")
ITEM_STATUSES = ("open", "next", "in progress", "done", "dropped")
# Enough for the plan picker of one project; a longer list is typed by id.
PICKED_PLANS = 200
SUGGESTION_STATUSES = ("open", "resolved", "wontfix")
DEFAULT_TOOL = "get_context"
NODE_MATCHES = 20
SUMMARY_CHARS = 60
GOING = ("running", "queued")
GLOBAL_LABEL = "global, listed under every project"
NO_SUCH_PAGE = "There is no such page here."


def instant(value: object) -> object:
    """Spell a timestamp as the API spells it; pass anything else through."""
    return jsjson.instant(value) if isinstance(value, datetime) else value


def field_kind(schema: dict[str, Any]) -> str:
    """Say how the Ask form reads one argument of a tool."""
    kind = schema.get("type")
    if kind in ("number", "integer"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "array":
        listed = (schema.get("items") or {}).get("type") == "string"
        return "list" if listed else "json"
    return "json" if kind == "object" else "text"


templates = Environment(
    loader=FileSystemLoader(HERE / "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
templates.globals.update(
    view.HELPERS, nav=NAV, dict=dict, instant=instant, field_kind=field_kind
)

router = APIRouter()


def render(name: str, code: int = 200, /, **context: object) -> HTMLResponse:
    """Render one template; a page may name anything in its context."""
    return HTMLResponse(templates.get_template(name).render(**context), code)


def failed(error: Exception, section: str = "") -> HTMLResponse:
    """Say what went wrong, on the page it went wrong on."""
    if isinstance(error, HttpError):
        status, message = error.status, error.message
    else:
        log.exception("page failed", exc_info=error)
        status, message = 503, str(error)
    return render(
        "failed.html", status, section=section, message=message, missing=False
    )


def no_such_page() -> HTMLResponse:
    """Answer an address no page lives at."""
    return render("failed.html", 404, section="", message=NO_SUCH_PAGE, missing=True)


def heard(path: str) -> dict[str, Any]:
    """Ask the API for one view, or an empty one when it does not answer."""
    try:
        answer = call("GET", path)
    except (UpstreamError, ValueError):
        return {}
    return answer if isinstance(answer, dict) else {}


def offset_of(request: Request, name: str = "offset") -> int:
    """Read a page offset out of the address, zero when it is not one."""
    value = number(read_query(request, name) or "0")
    return int(value) if whole(value) and value >= 0 else 0


def statuses_for(vocabulary: tuple[str, ...]) -> Callable[[str], list[str]]:
    """Return what lists a record's status first, then the usual ones."""
    return lambda status: list(dict.fromkeys([status, *vocabulary]))


def targets() -> list[dict[str, Any]]:
    """Return every project a record can be filed under."""
    return db.query(sql.PLAN_TARGETS)


def kind_of(project: dict[str, Any]) -> str:
    """Say which of the three lists a project belongs on."""
    if project["name"].startswith("_"):
        return "system"
    return "organizations" if project["type"] == "organization" else "indexed"


def _sorted(
    rows: list[dict[str, Any]], key: str, descending: bool
) -> list[dict[str, Any]]:
    field = "stale_seconds" if key == "indexed" else key
    # Never indexed has no age, so it settles last whichever way the column points.
    known = [row for row in rows if row[field] is not None]
    known.sort(key=lambda row: row[field], reverse=descending)
    return known + [row for row in rows if row[field] is None]


@router.get("/ui/lamps")
def lamps(request: Request) -> HTMLResponse:
    """Draw the embed and summarize lamps, for every project or for one."""
    project = read_query(request, "project")
    to = "/settings"
    if project is not None:
        to = view.address(f"/projects/{view.component(project)}", tab="settings")
    counted = project is None
    drawn = [
        (label, view.lamp(view.enabled_rows(heard(path).get(key), project), counted))
        for label, path, key in (
            ("embed", "/embeddings", "embeddings"),
            ("summarize", "/summaries", "summaries"),
        )
    ]
    return render("lamps.html", lamps=drawn, to=to)


@router.get("/ui/projects/{name}/index")
def index_control(name: str, request: Request) -> HTMLResponse:
    """Draw one project's index buttons, following a run while one is going."""
    try:
        job = projects.index_state(name)
    except HttpError:
        job = None
    going = job is not None and job.get("status") in GOING
    wrong = None
    if job is not None and job.get("status") == "failed":
        lines = [] if job.get("error") is None else [job["error"]]
        for one in job.get("skipped") or []:
            lines.append(f"{one['project']}: skipped, {one['why']}")
        wrong = "\n".join(lines)
    watched = read_flag(request, "watched")
    answer = render(
        "index_control.html",
        project=name,
        slug=view.slug(name),
        subject=read_query(request, "what") or name,
        what=read_query(request, "what"),
        compact=read_flag(request, "compact"),
        job=job,
        going=going,
        queued=job is not None and job.get("status") == "queued",
        finished=watched and not going,
        wrong=wrong,
    )
    if watched and not going:
        answer.headers["HX-Trigger"] = "index-finished"
    return answer


def _holding(project: dict[str, Any]) -> str | None:
    """Say why an organization refuses to be dropped or relabelled, if it does."""
    if project["type"] != "organization" or project["members"] == 0:
        return None
    held = project["members"]
    return (
        f"{project['name']} holds {held} "
        f"project{'' if held == 1 else 's'}. Take them out first: an "
        "organization that holds something stays one, and stays."
    )


@router.get("/ui/projects/{name}/drop")
def drop_modal(name: str) -> HTMLResponse:
    """Ask before a project is dropped, with what the drop would cost."""
    try:
        report = projects.drop_report(name)
        blocked = _holding(db.query(sql.PROJECT, [name])[0])
    except HttpError as error:
        return render("failed.html", error.status, message=error.message, missing=False)
    return render("drop_modal.html", report=report, blocked=blocked)


@router.get("/ui/node-options")
def node_options(request: Request) -> HTMLResponse:
    """List the nodes of a project matching what was typed, for a datalist."""
    field = read_query(request, "field") or ""
    named = re.search(r"name=([\w-]+)", read_query(request, "from") or "")
    project = (read_query(request, named.group(1)) if named else None) or ""
    typed = (read_query(request, field) or "").strip()
    options = ['<option value="./">the whole tree</option>']
    if project.strip() != "" and typed not in ("", "./"):
        found = db.query(
            sql.NODES,
            [project.strip(), nodes.pattern(typed), None, None, NODE_MATCHES, 0],
        )
        template = templates.from_string(
            '<option value="{{ node.id }}">{{ node.type }}'
            "{% if node.summary %}: {{ node.summary[:cut] }}{% endif %}</option>"
        )
        options += [template.render(node=node, cut=SUMMARY_CHARS) for node in found]
    return HTMLResponse("".join(options))


@router.get("/projects")
def projects_page(request: Request) -> HTMLResponse:
    """List the projects: the trees, the organizations, the built-in ones."""
    try:
        items = projects.listing()["items"]
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/projects")
    tab = read_query(request, "tab")
    tab = tab if tab in PROJECT_TABS else "indexed"
    q = read_query(request, "q")
    sort = read_query(request, "sort")
    direction = read_query(request, "dir")
    descending = direction != "asc"
    needle = (q or "").strip().lower()
    shown = [
        one
        for one in items
        if kind_of(one) == tab
        and (
            needle == ""
            or needle in one["name"].lower()
            or needle in (one["root_path"] or "").lower()
        )
    ]
    if sort in SORTABLE:
        shown = _sorted(shown, sort, descending)
    counts = {name: sum(kind_of(one) == name for one in items) for name in PROJECT_TABS}
    return render(
        "projects.html",
        section="/projects",
        tab=tab,
        q=q,
        sort=sort,
        dir=direction,
        descending=descending,
        any=bool(items),
        shown=shown,
        counts=counts,
        project_types=projects.PROJECT_TYPES,
    )


def _row_of(heard_: dict[str, Any], key: str, project: str) -> dict[str, Any] | None:
    rows = heard_.get(key) or []
    return next((one for one in rows if one["project"] == project), None)


def _overview(name: str, project: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if project["type"] == "organization":
        held = projects.members(name).get("members") or []
        taken = {one["project"] for one in held} | {name}
        context["members"] = held
        context["candidates"] = [
            one["name"] for one in targets() if one["name"] not in taken
        ]
    else:
        context["summary"] = _row_of(heard("/summaries"), "summaries", name)
        context["embedding"] = _row_of(heard("/embeddings"), "embeddings", name)
    return context


def _nodes(name: str, request: Request) -> dict[str, Any]:
    kind = read_query(request, "type")
    q = read_query(request, "q")
    selected = read_query(request, "node")
    source = read_flag(request, "source")
    context: dict[str, Any] = {
        "type": kind,
        "q": q,
        "selected": selected,
        "source": source,
        "nodes": nodes.nodes(name, q, kind, None, DEFAULT_LIMIT, offset_of(request)),
        "record_pages": RECORD_PAGES,
    }
    if selected is not None:
        try:
            context["node"] = nodes.node(name, selected, source)
        except HttpError as error:
            context["node_error"] = error.message
        context["known"] = records.knowledge(name, selected)
        context["neighbors"] = nodes.neighbors(
            name, selected, "both", DEFAULT_LIMIT, offset_of(request, "noffset")
        )
    return context


def _links(name: str, project: dict[str, Any], request: Request) -> dict[str, Any]:
    depth = number(read_query(request, "depth") or "1")
    depth = int(depth) if depth in (1, 2, 3) else 1
    asked = read_query(request, "trace")
    context: dict[str, Any] = {
        "organization": project["type"] == "organization",
        "depth": depth,
        "asked": asked,
        "incoming": read_flag(request, "incoming"),
        "kinds": LINK_KINDS,
        "relations": LINK_RELATIONS,
        "ways": WAYS,
        "others": [
            one["name"]
            for one in targets()
            if one["type"] != "organization" and one["name"] != name
        ],
    }
    try:
        context["links"] = anyio.from_thread.run(links.links, name, str(depth), None)
    except HttpError as error:
        context["links_error"] = error.message
        return context
    if asked is not None:
        try:
            context["trace"] = anyio.from_thread.run(links.trace, name, asked, None)
        except HttpError as error:
            context["trace_error"] = error.message
    return context


def _settings(name: str, project: dict[str, Any]) -> dict[str, Any]:
    embeddings = heard("/embeddings")
    summaries = heard("/summaries")
    return {
        "organization": project["type"] == "organization",
        "settings": projects.settings(name),
        "file_types": projects.file_types(name)["items"],
        "schedule": heard(f"/projects/{view.component(name)}/schedule"),
        "features": heard(f"/projects/{view.component(name)}/features").get("features")
        or {},
        "embeddings": embeddings.get("embeddings"),
        "summaries": summaries.get("summaries"),
        "embedding_model": embeddings.get("model"),
        "embedding_row": _row_of(embeddings, "embeddings", name),
        "summary_row": _row_of(summaries, "summaries", name),
    }


@router.get("/projects/{name}")
def project_page(name: str, request: Request) -> HTMLResponse:
    """Show one project, one tab of it at a time."""
    try:
        project = projects.detail(name)
        if name.startswith("_"):
            offered: tuple[str, ...] = ("overview",)
        elif project["type"] == "organization":
            offered = ORGANIZATION_TABS
        else:
            offered = TABS
        asked = read_query(request, "tab") or "overview"
        tab = asked if asked in offered else "overview"
        held = projects.organizations(name)
        context: dict[str, Any] = {
            "section": "/projects",
            "project": project,
            "offered": offered,
            "tab": tab,
            "here": f"/projects/{view.component(name)}",
            "types": list(dict.fromkeys([project["type"], *projects.PROJECT_TYPES])),
            "holding": _holding(project),
            "held": held,
            "free": [
                one["name"]
                for one in targets()
                if one["type"] == "organization"
                and one["name"] != name
                and one["name"] not in held["organizations"]
            ],
        }
        if tab == "overview":
            context.update(_overview(name, project))
        elif tab == "graph":
            context["forced"] = read_flag(request, "forced")
        elif tab == "nodes":
            context.update(_nodes(name, request))
        elif tab == "files":
            q = read_query(request, "q")
            context["q"] = q
            context["files"] = nodes.files(name, q, DEFAULT_LIMIT, offset_of(request))
        elif tab == "links":
            context.update(_links(name, project, request))
        elif tab == "settings":
            context.update(_settings(name, project))
        elif tab == "skills":
            context["skills"] = skills.project_skills(name)
        elif tab == "roadmaps":
            context["roadmaps"] = roadmaps.roadmaps(name, None)
            context["picked"] = plans.plans(name, None, None, None, PICKED_PLANS, 0)
            context["roadmap_statuses"] = statuses_for(PLAN_STATUSES)
            context["item_statuses"] = statuses_for(ITEM_STATUSES)
        elif tab == "plans":
            status = read_query(request, "status")
            kind = read_query(request, "type")
            context["status"] = status
            context["type"] = kind
            context["facets"] = plans.facets()
            context["plans"] = plans.plans(
                name, status, kind, None, DEFAULT_LIMIT, offset_of(request), True
            )
            context["statuses_for"] = statuses_for(PLAN_STATUSES)
        elif tab == "prompts":
            status = read_query(request, "status")
            context["status"] = status
            context["statuses"] = prompts.facets()["statuses"]
            context["prompts"] = prompts.prompts(
                name, status, None, None, DEFAULT_LIMIT, offset_of(request)
            )
        elif tab == "failures":
            given_up = projects.passed(name, "failures")
            context["sections"] = (
                ("summaries", "Summarizing", given_up.get("summaries") or []),
                ("embeddings", "Embedding", given_up.get("embeddings") or []),
            )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/projects")
    return render("project.html", **context)


@router.get("/plans/{plan_id:path}")
def plan_page(plan_id: str, request: Request) -> HTMLResponse:
    """Show one plan, rendered or being edited."""
    try:
        plan = plans.plan(plan_id)
        tagged = [] if plan["project"] is None else [plan["project"]]
        return render(
            "plan.html",
            section="/projects",
            plan=plan,
            editing=read_flag(request, "edit"),
            nodes=records.record_nodes("plan", plan_id),
            prompts=prompts.of_plan(plan_id),
            items=roadmaps.of_plan(plan_id),
            targets=[
                {"value": "", "label": GLOBAL_LABEL},
                *view.project_entries(targets(), tagged),
            ],
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/projects")


@router.get("/prompts/{prompt_id:path}")
def prompt_page(prompt_id: str, request: Request) -> HTMLResponse:
    """Show one prompt, rendered or being edited."""
    try:
        prompt = prompts.prompt(prompt_id)
        return render(
            "prompt.html",
            section="/projects",
            prompt=prompt,
            editing=read_flag(request, "edit"),
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/projects")


@router.get("/memories")
def memories_page(request: Request) -> HTMLResponse:
    """List the memories."""
    try:
        about = read_query(request, "about") or plans.ALL
        tag = read_query(request, "tag")
        q = read_query(request, "q")
        return render(
            "memories.html",
            section="/memories",
            about=about,
            tag=tag,
            q=q,
            facets=memories.facets(),
            memories=memories.memories(
                about, tag, q, DEFAULT_LIMIT, offset_of(request)
            ),
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/memories")


@router.get("/memories/{memory_id:path}")
def memory_page(memory_id: str, request: Request) -> HTMLResponse:
    """Show one memory, rendered or being edited."""
    try:
        return render(
            "memory.html",
            section="/memories",
            row=memories.memory(memory_id),
            editing=read_flag(request, "edit"),
            nodes=records.record_nodes("memory", memory_id),
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/memories")


@router.get("/suggestions")
def suggestions_page(request: Request) -> HTMLResponse:
    """List the suggestions, and where they pile up."""
    try:
        about = read_query(request, "about") or plans.ALL
        status = read_query(request, "status")
        kind = read_query(request, "kind")
        q = read_query(request, "q")
        by = read_query(request, "by") or "kind"
        return render(
            "suggestions.html",
            section="/suggestions",
            about=about,
            status=status,
            kind=kind,
            q=q,
            by=by,
            groups=records.groups(by, status)["groups"],
            facets=suggestions.facets(),
            suggestions=suggestions.suggestions(
                about, status, kind, q, DEFAULT_LIMIT, offset_of(request)
            ),
            statuses_for=statuses_for(SUGGESTION_STATUSES),
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/suggestions")


@router.get("/suggestions/{suggestion_id:path}")
def suggestion_page(suggestion_id: str, request: Request) -> HTMLResponse:
    """Show one suggestion, rendered or being edited."""
    try:
        row = suggestions.suggestion(suggestion_id)
        return render(
            "suggestion.html",
            section="/suggestions",
            row=row,
            editing=read_flag(request, "edit"),
            nodes=records.record_nodes("suggestion", suggestion_id),
            statuses=statuses_for(SUGGESTION_STATUSES)(row["status"]),
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/suggestions")


@router.get("/skills")
def skills_page(request: Request) -> HTMLResponse:
    """List the skills the server hands out."""
    try:
        owner = read_query(request, "owner")
        return render(
            "skills.html",
            section="/skills",
            owner=owner,
            owners=[one["name"] for one in targets()],
            skills=skills.skills(owner),
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/skills")


@router.get("/skills/{skill_id}")
def skill_page(skill_id: str) -> HTMLResponse:
    """Show one skill and its text."""
    try:
        return render(
            "skill.html", section="/skills", row=skills.skill(skills.read_id(skill_id))
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/skills")


@router.get("/queues")
def queues_page() -> HTMLResponse:
    """Show what the two model queues are doing, and how far each project is."""
    try:
        folded = queues.fold(call("GET", "/summaries"), call("GET", "/embeddings"))
    except UpstreamError as error:
        return failed(HttpError(error.status, error.message), "/queues")
    return render("queues.html", section="/queues", folded=folded)


@router.get("/settings")
def settings_page() -> HTMLResponse:
    """Show the defaults every project starts from."""
    try:
        return render(
            "settings.html",
            section="/settings",
            level=settings.settings(),
            features=heard("/projects/_settings/features").get("features") or {},
            embeddings=heard("/embeddings").get("embeddings"),
            summaries=heard("/summaries").get("summaries"),
        )
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/settings")


@router.get("/")
async def ask_page(request: Request) -> HTMLResponse:
    """Offer the tools of the MCP server, one form each."""
    project = read_query(request, "project") or ""
    picked = read_query(request, "tool") or DEFAULT_TOOL
    context: dict[str, Any] = {"section": "/", "project": project, "picked": picked}
    try:
        known = await run_in_threadpool(targets)
    except Exception as error:  # noqa: BLE001 - shown on the page
        return failed(error, "/")
    context["targets"] = [
        {"value": "", "label": "none, the bare /mcp address"},
        *view.project_entries(known),
    ]
    try:
        tools = await mcpclient.list_tools(project)
    except HttpError as error:
        tools = []
        context["tools_error"] = error.message
    context["tool_entries"] = [
        {"value": tool["name"], "label": tool["name"], "group": tool["group"]}
        for tool in tools
    ]
    context["tool"] = next((tool for tool in tools if tool["name"] == picked), None)
    return render("ask.html", **context)
