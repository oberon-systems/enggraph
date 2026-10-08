"""Refuse a request from a host this service was not told about."""

from __future__ import annotations

import os

from fastapi import Request

from enggraph.web.args import HttpError

WRITES = {"POST", "PUT", "PATCH", "DELETE"}
PORT = int(os.environ.get("PORT", "3002"))


def allowed_hosts(port: int) -> set[str]:
    """Return the Host headers answered: WEB_ALLOWED_HOSTS, or the local ones."""
    configured = os.environ.get("WEB_ALLOWED_HOSTS", "")
    if configured.strip() != "":
        return {
            entry.strip().lower() for entry in configured.split(",") if entry.strip()
        }
    return {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}", "web:3002"}


HOSTS = allowed_hosts(PORT)


def guard(request: Request) -> None:
    """Hold a request to a known host, and a write to its own origin and JSON."""
    host = request.headers.get("host", "").lower()
    if "*" not in HOSTS and host not in HOSTS:
        raise HttpError(
            403,
            f'Host "{host}" is not allowed. Add it to GATEWAY_HOSTS in the '
            "stack's .env, then recreate the containers with `make down && "
            "make up`; * there answers to any host. Running this service "
            "outside compose, the variable read here is WEB_ALLOWED_HOSTS.",
        )
    if request.method not in WRITES:
        return
    # A form cannot send application/json and a fetch that can is preflighted,
    # so a same-origin check and the content type leave no cross-site write.
    origin = request.headers.get("origin", "")
    if origin != "" and origin.lower() != f"http://{host}":
        raise HttpError(403, f'Cross-origin write from "{origin}" refused')
    kind = request.headers.get("content-type", "")
    if not kind.lower().startswith("application/json"):
        raise HttpError(415, "Writes must carry a application/json body")
