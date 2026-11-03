"""Where host agents enroll, rotate their credential and hold their socket.

The gateway exposes this as `/agent/agents/...` with no check of its own, since
agents sign every request. What travels over the socket beyond the greeting and
the pings is not decided here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketException,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from enggraph.core import agentauth
from enggraph.core.agentauth import AgentAuthError, Credential, MasterKeyError

log = logging.getLogger(__name__)

PROTOCOL = 1
# How long a socket may stay silent before its state is read again.
STATE_CHECK_SECONDS = 30
HELLO_SECONDS = 30
# An agent pings every 20 s; a peer quiet for this long is gone, whatever TCP says.
SILENT_SECONDS = 90
MAX_EXPORTS = 256
MAX_FRAME_CHARS = 1024 * 1024

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Label = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")]


class StrictModel(BaseModel):
    """A body that names nothing this API does not read."""

    model_config = ConfigDict(extra="forbid")


class ExportIn(StrictModel):
    """One directory an agent offers."""

    name: Name
    path: Annotated[str, Field(min_length=1, max_length=4096)]


class RegisterIn(StrictModel):
    """Who asks to enroll, and what it offers."""

    name: Name
    labels: Annotated[list[Label], Field(max_length=16)] = []
    agent_id: uuid.UUID | None = None
    exports: Annotated[list[ExportIn], Field(max_length=MAX_EXPORTS)] = []


class ClaimIn(StrictModel):
    """The one-time code a registration was answered with."""

    code: Annotated[str, Field(min_length=1, max_length=128)]


class ExportsIn(StrictModel):
    """Everything an agent offers now."""

    exports: Annotated[list[ExportIn], Field(max_length=MAX_EXPORTS)]


router = APIRouter(prefix="/agents")


def unauthorized() -> HTTPException:
    """Return the one refusal a bad signature gets, saying nothing of why."""
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorized")


def no_master_key(error: MasterKeyError) -> HTTPException:
    """Return a 503: the stack is misconfigured, and the agent should wait."""
    log.error("%s", error)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no master key")


def credential_out(credential: Credential, key: str) -> dict[str, str | int]:
    """Spell a credential: the header goes verbatim, the key is shown only once."""
    return {
        "credential": credential.header,
        "expires_at": credential.expires_at,
        "key": key,
    }


def exports_of(exports: list[ExportIn]) -> list[dict[str, str]]:
    """Return announced exports as plain values."""
    return [{"name": entry.name, "path": entry.path} for entry in exports]


async def raw_body(request: Request) -> bytes:
    """Return the bytes that were signed."""
    return await request.body()


def require_agent(
    agent_id: uuid.UUID,
    request: Request,
    body: Annotated[bytes, Depends(raw_body)],
    x_credential: Annotated[str, Header()] = "",
    x_timestamp: Annotated[str, Header()] = "",
    x_signature: Annotated[str, Header()] = "",
) -> Credential:
    """Return the credential of the agent the path names, once its request checks."""
    try:
        credential = agentauth.verify_request(
            str(agent_id),
            x_credential,
            x_timestamp,
            x_signature,
            request.method,
            agentauth.signed_path(request.url.path),
            body,
        )
    except MasterKeyError as error:
        raise no_master_key(error) from error
    if credential is None:
        raise unauthorized()
    return credential


SignedAgent = Annotated[Credential, Depends(require_agent)]


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn) -> dict[str, str]:
    """Queue an agent for an operator and hand it a one-time claim code."""
    known = None if body.agent_id is None else str(body.agent_id)
    try:
        code = agentauth.register(
            body.name, body.labels, known, exports_of(body.exports)
        )
    except AgentAuthError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    return {"claim_code": code}


@router.post("/claim")
def claim(body: ClaimIn) -> Response:
    """Answer 409 while waiting, 401 once refused or expired, else the credential.

    The answer is signed under the code, so the agent can tell that what it was
    handed came from the stack it registered with.
    """
    try:
        claimed = agentauth.claim(body.code)
    except agentauth.WaitingError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "waiting") from error
    except MasterKeyError as error:
        raise no_master_key(error) from error
    if claimed is None:
        raise unauthorized()
    credential, key = claimed
    content = json.dumps(
        {
            "agent_id": credential.owner_id,
            "credential": credential_out(credential, key),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return Response(
        content,
        media_type="application/json",
        headers={"X-Signature": agentauth.claim_signature(body.code, content)},
    )


@router.post("/{agent_id}/credential")
def refresh(agent: SignedAgent) -> dict[str, str | int]:
    """Hand the agent a fresh credential in a pending generation."""
    try:
        return credential_out(*agentauth.refresh(agent.owner_id))
    except MasterKeyError as error:
        raise no_master_key(error) from error


@router.put("/{agent_id}/exports")
def put_exports(body: ExportsIn, agent: SignedAgent) -> dict[str, Any]:
    """Store what the agent offers now, and say where each export stands."""
    try:
        rows = agentauth.set_exports(agent.owner_id, exports_of(body.exports))
    except AgentAuthError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    return {"exports": rows}


def socket_credential(agent_id: uuid.UUID, websocket: WebSocket) -> Credential:
    """Return the credential of a socket's upgrade, signed as a GET with no body."""
    headers = websocket.headers
    try:
        credential = agentauth.verify_request(
            str(agent_id),
            headers.get("x-credential", ""),
            headers.get("x-timestamp", ""),
            headers.get("x-signature", ""),
            "GET",
            agentauth.signed_path(websocket.url.path),
            b"",
        )
    except MasterKeyError as error:
        log.error("%s", error)
        credential = None
    if credential is None:
        raise WebSocketException(status.WS_1008_POLICY_VIOLATION)
    return credential


def frame_of(message: dict[str, object]) -> dict[str, object] | None:
    """Read a text frame as JSON; anything else is not for this protocol."""
    text = message.get("text")
    if not isinstance(text, str) or len(text) > MAX_FRAME_CHARS:
        return None
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


async def greet(websocket: WebSocket, credential: Credential) -> bool:
    """Wait for the hello that opens a socket, note it and answer it."""
    try:
        async with asyncio.timeout(HELLO_SECONDS):
            message = await websocket.receive()
    except TimeoutError:
        return False
    frame = frame_of(message) if message["type"] == "websocket.receive" else None
    if frame is None or frame.get("type") != "hello":
        return False
    version = frame.get("version")
    announced = version[:64] if isinstance(version, str) else None
    if not await asyncio.to_thread(agentauth.seen, credential, announced):
        return False
    await websocket.send_json({"type": "hello", "protocol": PROTOCOL})
    return True


async def hold(websocket: WebSocket, credential: Credential) -> None:
    """Answer pings until the agent leaves, its credential lapses or it is cut off."""
    # One receive lives across wakes: cancelling it could lose a frame.
    receiving = asyncio.ensure_future(websocket.receive())
    heard = agentauth.now()
    try:
        while True:
            left = credential.expires_at - agentauth.now()
            if left <= 0:
                await websocket.close(status.WS_1000_NORMAL_CLOSURE, "expired")
                return
            done, _ = await asyncio.wait(
                {receiving}, timeout=min(left, STATE_CHECK_SECONDS)
            )
            if not done:
                silent = agentauth.now() - heard > SILENT_SECONDS
                if silent or not await asyncio.to_thread(
                    agentauth.standing, credential
                ):
                    await websocket.close(status.WS_1008_POLICY_VIOLATION)
                    return
                continue
            message = receiving.result()
            if message["type"] == "websocket.disconnect":
                return
            heard = agentauth.now()
            receiving = asyncio.ensure_future(websocket.receive())
            frame = frame_of(message)
            if frame is None or frame.get("type") != "ping":
                continue
            if not await asyncio.to_thread(agentauth.seen, credential):
                await websocket.close(status.WS_1008_POLICY_VIOLATION)
                return
            await websocket.send_json({"type": "pong"})
    finally:
        receiving.cancel()


@router.websocket("/{agent_id}/socket")
async def socket(websocket: WebSocket, agent_id: uuid.UUID) -> None:
    """Hold one signed socket open; a refused upgrade never gets as far as accept."""
    credential = await asyncio.to_thread(socket_credential, agent_id, websocket)
    await websocket.accept()
    if not await greet(websocket, credential):
        await websocket.close(status.WS_1008_POLICY_VIOLATION)
        return
    await hold(websocket, credential)
