"""What a caller is told when something fails: a code and an id, never the text.

The text of a driver, a library or the operating system names tables, hosts
and paths. It goes to the log, under an id the answer carries.
"""

from __future__ import annotations

import logging
import secrets

NUL = "a string holds a NUL character"


def note(log: logging.Logger, code: str, error: BaseException) -> str:
    """Log an error with its traceback under a new id, and return the id."""
    error_id = secrets.token_hex(6)
    log.error("%s [error_id=%s]", code, error_id, exc_info=error)
    return error_id


def told(code: str, error_id: str) -> str:
    """Return the sentence an answer carries in place of the error."""
    return f"{code} (error id {error_id})"


def hidden(log: logging.Logger, code: str, error: BaseException) -> str:
    """Log an error and return the sentence that stands for it."""
    return told(code, note(log, code, error))


def ours(error: BaseException) -> bool:
    """Say whether an error's text was written in this tree for its reader."""
    # A plain RuntimeError is how the storage layer refuses; a subclass is not.
    return type(error) is RuntimeError


def nul(error: BaseException) -> bool:
    """Say whether the driver refused a string for the NUL character in it."""
    return isinstance(error, ValueError) and "NUL (0x00)" in str(error)
