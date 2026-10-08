"""What a tool says when it cannot answer, in words an agent can act on."""

from __future__ import annotations

import psycopg2


class ToolError(Exception):
    """A refusal whose message is the whole answer."""


def message(error: BaseException) -> str:
    """Return the sentence the previous server showed for an error."""
    if isinstance(error, psycopg2.Error) and error.diag.message_primary:
        return str(error.diag.message_primary)
    return str(error)
