"""Read a request the way the routes expect it, and answer in JSON."""

from __future__ import annotations

import json
import math
import os
from typing import Annotated, Any

from fastapi import Depends, Request
from starlette.responses import Response

from enggraph.core import jsjson

# Where file text comes from: the API holds the mounts and the deny list.
API_URL = os.environ.get("API_URL", "http://worker-api:3003")
API_TOKEN = os.environ.get("API_TOKEN", "")

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
BODY_LIMIT = 2 * 1024 * 1024
URI_SAFE = "-_.!~*'()"


class _Missing:
    """A field the body does not carry, which is not the same as a null one."""

    def __bool__(self) -> bool:
        """Read as false, so `value or default` works."""
        return False


MISSING = _Missing()


class HttpError(Exception):
    """An error carrying the status the route should answer with."""

    def __init__(self, status: int, message: str) -> None:
        """Keep the status beside the message."""
        super().__init__(message)
        self.status = status
        self.message = message


def bad_request(message: str) -> HttpError:
    """Return a 400."""
    return HttpError(400, message)


def not_found(message: str) -> HttpError:
    """Return a 404."""
    return HttpError(404, message)


def reply(value: object, status: int = 200) -> Response:
    """Answer with JSON spelled as the previous server spelled it."""
    return Response(
        jsjson.dumps(value),
        status_code=status,
        headers={"content-type": "application/json; charset=utf-8"},
    )


async def read_body(request: Request) -> object:
    """Parse a JSON body, or answer an empty object when there is none."""
    kind = request.headers.get("content-type", "").lower()
    if not kind.startswith("application/json"):
        return {}
    raw = await request.body()
    if len(raw) > BODY_LIMIT:
        raise HttpError(413, "request entity too large")
    if raw.strip() == b"":
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise bad_request(f"The body is not JSON: {error}") from error
    if not isinstance(parsed, dict | list):
        raise bad_request("A JSON object body is required")
    return parsed


Body = Annotated[object, Depends(read_body)]


def number(raw: str) -> float:
    """Read a number out of text, or NaN when it is none."""
    try:
        return float(raw.strip() or "0")
    except ValueError:
        return math.nan


def whole(value: float) -> bool:
    """Tell whether a number is an integer."""
    return math.isfinite(value) and value == int(value)


def read_query(request: Request, name: str) -> str | None:
    """Return a query parameter, the last one when it is repeated."""
    values = request.query_params.getlist(name)
    if not values or values[-1].strip() == "":
        return None
    return values[-1]


def require_query(request: Request, name: str) -> str:
    """Return a query parameter, or refuse the request without it."""
    value = read_query(request, name)
    if value is None:
        raise bad_request(f'Query parameter "{name}" is required')
    return value


def read_flag(request: Request, name: str) -> bool:
    """Tell whether a query parameter switches something on."""
    return read_query(request, name) in ("1", "true")


def read_limit(request: Request) -> int:
    """Return the page size asked for, held to the maximum."""
    raw = read_query(request, "limit")
    if raw is None:
        return DEFAULT_LIMIT
    value = number(raw)
    if not whole(value) or value < 1:
        raise bad_request('Query parameter "limit" must be a positive integer')
    return min(int(value), MAX_LIMIT)


def read_offset(request: Request) -> int:
    """Return how many rows to skip."""
    raw = read_query(request, "offset")
    if raw is None:
        return 0
    value = number(raw)
    if not whole(value) or value < 0:
        raise bad_request('Query parameter "offset" must be zero or more')
    return int(value)


def field(body: object, name: str) -> Any:  # noqa: ANN401
    """Return one field of a body, or MISSING when it is not there."""
    if isinstance(body, dict):
        return body.get(name, MISSING)
    return MISSING


def _object(body: object) -> None:
    if not isinstance(body, dict | list):
        raise bad_request("A JSON object body is required")


def read_body_string(body: object, name: str) -> str | None:
    """Return a string field, or None when the body leaves it out."""
    _object(body)
    value = field(body, name)
    if value is MISSING:
        return None
    if not isinstance(value, str):
        raise bad_request(f'Field "{name}" must be a string')
    return value


def read_body_number(body: object, name: str, low: int, high: int) -> int | None:
    """Return a whole number within bounds, or None when it is left out."""
    _object(body)
    value = field(body, name)
    # Null is a level with nothing of its own to add, the same as leaving it out.
    if value is MISSING or value is None:
        return None
    numeric = isinstance(value, int | float) and not isinstance(value, bool)
    if not numeric or not whole(value):
        raise bad_request(f'Field "{name}" must be a whole number')
    if value < low or value > high:
        raise bad_request(f'Field "{name}" must be between {low} and {high}')
    return int(value)


def read_body_enum(body: object, name: str, allowed: tuple[str, ...]) -> str | None:
    """Return a string field held to a vocabulary."""
    value = read_body_string(body, name)
    if value is None or value == "":
        return None
    if value not in allowed:
        raise bad_request(f'Field "{name}" must be one of {", ".join(allowed)}')
    return value


def require_body_string(body: object, name: str) -> str:
    """Return a string field, or refuse the request without it."""
    value = read_body_string(body, name)
    if value is None or value.strip() == "":
        raise bad_request(f'Field "{name}" is required')
    return value


def text(value: object) -> str:
    """Spell a value as JavaScript's String() does."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return jsjson.number(float(value))
    if isinstance(value, list):
        return ",".join("" if item is None else text(item) for item in value)
    return "[object Object]"


def count(value: str | int | None) -> int:
    """Read what the database counted back as a number."""
    return 0 if value is None else int(value)
