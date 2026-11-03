"""Agents: approving them, their exports and what those bind to, for operators."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.core import agentauth
from enggraph.web.args import (
    Body,
    HttpError,
    bad_request,
    not_found,
    read_body_enum,
    read_body_string,
    reply,
    require_body_string,
    require_query,
)
from enggraph.web.guard import require_operator

router = APIRouter()


def listed() -> dict[str, Any]:
    """Return what waits for a decision, and every agent with its exports."""
    require_operator()
    exports = agentauth.list_exports()
    agents = [
        {**agent, "exports": [e for e in exports if e["agent_id"] == agent["id"]]}
        for agent in agentauth.list_agents()
    ]
    return {"registrations": agentauth.list_registrations(), "agents": agents}


def approve(body: object) -> dict[str, str]:
    """Let a waiting agent claim its credential."""
    principal = require_operator()
    agent_id = require_body_string(body, "agent_id")
    if not agentauth.approve_registration(agent_id, principal.subject):
        raise not_found(f'No registration of "{agent_id}" waits. Nothing was approved.')
    return {"approved": agent_id}


def refuse(body: object) -> dict[str, str]:
    """Forget a registration; its claim code opens nothing."""
    require_operator()
    agent_id = require_body_string(body, "agent_id")
    if not agentauth.refuse_registration(agent_id):
        raise not_found(f'No registration of "{agent_id}". Nothing was refused.')
    return {"refused": agent_id}


def set_state(body: object) -> dict[str, str]:
    """Enable or disable an agent."""
    require_operator()
    agent_id = require_body_string(body, "agent_id")
    state = read_body_enum(body, "state", agentauth.STATES)
    if state is None:
        raise bad_request('Field "state" is required')
    if not agentauth.set_state(agent_id, state):
        raise not_found(f'No approved agent "{agent_id}". Nothing was changed.')
    return {"agent": agent_id, "state": state}


def revoke(body: object) -> dict[str, str]:
    """End the credential an agent holds; it has to enroll again."""
    require_operator()
    agent_id = require_body_string(body, "agent_id")
    if not agentauth.revoke(agent_id):
        raise not_found(f'"{agent_id}" holds no credential. Nothing was revoked.')
    return {"revoked": agent_id}


def decide(body: object) -> dict[str, str]:
    """Approve or refuse one export of an agent."""
    principal = require_operator()
    agent_id = require_body_string(body, "agent_id")
    export = require_body_string(body, "export")
    decision = read_body_enum(body, "decision", agentauth.DECISIONS)
    if decision is None:
        raise bad_request('Field "decision" is required')
    if not agentauth.decide_export(agent_id, export, decision, principal.subject):
        raise not_found(f'Agent "{agent_id}" announces no export "{export}".')
    return {"export": export, "decision": decision}


def bind(body: object) -> dict[str, Any]:
    """Bind an approved export to the project on its path, or to a new one."""
    principal = require_operator()
    try:
        return agentauth.bind_export(
            require_body_string(body, "agent_id"),
            require_body_string(body, "export"),
            principal.subject,
            read_body_string(body, "project") or None,
            read_body_enum(body, "type", agentauth.TREE_TYPES)
            or agentauth.TREE_TYPES[0],
        )
    except agentauth.AgentAuthError as error:
        raise HttpError(409, str(error)) from error


def unbind(project: str) -> dict[str, str]:
    """Take a project's binding back."""
    require_operator()
    if not agentauth.unbind(project):
        raise not_found(f'Project "{project}" is bound to no export.')
    return {"unbound": project}


@router.get("/agents")
def _agents() -> Response:
    return reply(listed())


@router.post("/agents/approve")
def _approve(body: Body) -> Response:
    return reply(approve(body))


@router.post("/agents/refuse")
def _refuse(body: Body) -> Response:
    return reply(refuse(body))


@router.post("/agents/state")
def _state(body: Body) -> Response:
    return reply(set_state(body))


@router.post("/agents/revoke")
def _revoke(body: Body) -> Response:
    return reply(revoke(body))


@router.post("/agents/exports/decide")
def _decide(body: Body) -> Response:
    return reply(decide(body))


@router.post("/agents/bind")
def _bind(body: Body) -> Response:
    return reply(bind(body), 201)


@router.delete("/agents/binding")
def _unbind(request: Request) -> Response:
    return reply(unbind(require_query(request, "project")))
