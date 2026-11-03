"""Tokens: the credentials agents and scripts carry, for operators alone."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from enggraph.core import agentauth, authn
from enggraph.web.args import (
    Body,
    bad_request,
    field,
    not_found,
    read_body_enum,
    read_body_number,
    read_body_string,
    reply,
    require_body_string,
    require_query,
)
from enggraph.web.guard import require_operator

MAX_DAYS = 3650

router = APIRouter()


def listed() -> list[dict[str, Any]]:
    """List every token, newest first, without its hash."""
    require_operator()
    return authn.list_tokens()


def strings(body: object, name: str, fallback: list[str]) -> list[str]:
    """Read a list of strings, or a comma-separated string, from a body."""
    value = field(body, name)
    if not value:
        return fallback
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return value
    raise bad_request(f'Field "{name}" must be a list of strings')


def ticked(body: object) -> list[str]:
    """Read the scopes the form ticked, one `scope_<name>` checkbox each."""
    return ["read"] + [
        scope
        for scope in authn.SCOPES
        if scope != "read" and field(body, f"scope_{scope}") is True
    ]


def host_of(body: object) -> str | None:
    """Read the agent a token is bound to, which must be an approved one."""
    host = (read_body_string(body, "host") or "").strip() or None
    if host is not None and not agentauth.known_agent(host):
        raise bad_request(f'No approved agent "{host}" to bind a token to')
    return host


def issue(body: object) -> dict[str, Any]:
    """Issue a token; its text is in the answer once and stored nowhere."""
    principal = require_operator()
    kind = read_body_enum(body, "kind", authn.KINDS) or "mcp"
    role = read_body_enum(body, "role", authn.ROLES) or "reader"
    if kind == "operator":
        # Only an operator token opens a session at the login form.
        role = "operator"
    try:
        text, row = authn.issue_token(
            kind,
            require_body_string(body, "name"),
            role,
            strings(body, "scopes", ticked(body)),
            strings(body, "projects", [authn.ALL_PROJECTS]),
            read_body_number(body, "days", 1, MAX_DAYS),
            principal.subject,
            host_of(body),
        )
    except authn.AuthError as error:
        raise bad_request(str(error)) from error
    return {"said": text, "token": row}


def set_host(token_id: str, body: object) -> dict[str, Any]:
    """Bind a token to an agent's host, or free it with an empty one."""
    require_operator()
    host = host_of(body)
    if not authn.set_token_host(token_id, host):
        raise not_found(f'No token "{token_id}". Nothing was changed.')
    return {"token": token_id, "host": host}


def revoke(token_id: str) -> dict[str, Any]:
    """Revoke a token and end the sessions it opened."""
    require_operator()
    if not authn.revoke_token(token_id):
        raise not_found(f'No active token "{token_id}". Nothing was revoked.')
    return {"revoked": token_id}


@router.get("/tokens")
def _tokens() -> Response:
    return reply(listed())


@router.post("/tokens")
def _issue(body: Body) -> Response:
    return reply(issue(body), 201)


@router.patch("/token")
def _set_host(request: Request, body: Body) -> Response:
    return reply(set_host(require_query(request, "id"), body))


@router.delete("/token")
def _revoke(request: Request) -> Response:
    return reply(revoke(require_query(request, "id")))
