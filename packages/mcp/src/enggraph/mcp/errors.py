"""What a tool says when it cannot answer, in words an agent can act on."""

from __future__ import annotations

import logging

import psycopg2

from enggraph.core import failures, trees

LOG = logging.getLogger(__name__)
DATABASE_FAILED = "the database refused the request or does not answer"
TOOL_FAILED = "the tool failed"


class ToolError(Exception):
    """A refusal whose message is the whole answer."""


def message(error: BaseException) -> str:
    """Return what an agent is told: a refusal's own words, or a code and an id."""
    written = (ToolError, trees.TreeUnavailable, trees.TreeRefused)
    if isinstance(error, written) or failures.ours(error):
        return str(error)
    if failures.nul(error):
        return failures.NUL
    code = DATABASE_FAILED if isinstance(error, psycopg2.Error) else TOOL_FAILED
    return failures.hidden(LOG, code, error)
