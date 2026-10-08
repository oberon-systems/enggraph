"""The global level: its defaults, its features and the queues behind them."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from starlette.responses import Response

from enggraph.core import db
from enggraph.web import queries as sql
from enggraph.web.args import Body, read_body_string, reply
from enggraph.web.features import redact_keys
from enggraph.web.routes.projects import (
    SETTINGS_PROJECT,
    save_feature,
    save_ignore,
    save_indexing,
)
from enggraph.web.upstream import ask

router = APIRouter()


def settings() -> dict[str, Any]:
    """Return the global level, its tokens taken out."""
    rows = db.query(sql.PROJECT_LEVEL_SETTINGS, [SETTINGS_PROJECT])
    if not rows:
        return {"ignore_patterns": None, "settings": {}, "updated_at": None}
    return {**rows[0], "settings": redact_keys(rows[0]["settings"])}


def probe(queue: str, body: object) -> Any:  # noqa: ANN401
    """Test a server URL before it is stored."""
    url = read_body_string(body, "url") or ""
    return ask("POST", f"/{queue}/probe", None, {"url": url})


def retry(queue: str, body: object) -> Any:  # noqa: ANN401
    """Put the files that gave up back in a queue."""
    project = read_body_string(body, "project") or ""
    return ask("POST", f"/{queue}/retry", None, {"project": project})


@router.get("/settings")
def _settings() -> Response:
    return reply(settings())


@router.put("/settings")
def _save_ignore(body: Body) -> Response:
    return reply(save_ignore(SETTINGS_PROJECT, body))


@router.put("/settings/indexing")
def _save_indexing(body: Body) -> Response:
    return reply(save_indexing(SETTINGS_PROJECT, body, True))


@router.put("/settings/features/{feature}")
def _save_feature(feature: str, body: Body) -> Response:
    return reply(save_feature(SETTINGS_PROJECT, feature, body, True))


@router.get("/embeddings")
def _embeddings() -> Response:
    return reply(ask("GET", "/embeddings"))


@router.post("/summaries/probe")
def _probe_summaries(body: Body) -> Response:
    return reply(probe("summaries", body))


@router.post("/embeddings/probe")
def _probe_embeddings(body: Body) -> Response:
    return reply(probe("embeddings", body))


@router.get("/summaries")
def _summaries() -> Response:
    return reply(ask("GET", "/summaries"))


@router.get("/settings/features")
def _features() -> Response:
    return reply(ask("GET", "/projects/_settings/features"))


@router.post("/embeddings/retry")
def _retry_embeddings(body: Body) -> Response:
    return reply(retry("embeddings", body))


@router.post("/summaries/retry")
def _retry_summaries(body: Body) -> Response:
    return reply(retry("summaries", body))
