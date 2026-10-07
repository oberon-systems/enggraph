"""Read a file of an indexed tree from the tree rather than from the graph.

The one place that turns a project and a project-relative path into text for
somebody else to see, so the containment check and the deny list exist once.
Everything that serves file text - the summarization queue, the content
endpoint - comes through here.
"""

from __future__ import annotations

import fnmatch
import posixpath

from enggraph.core import trees
from enggraph.core.config import CONTENT_DENIED_NAMES

DENIED = "denied by name"
ESCAPES = "outside the project"
UNMOUNTED = "the project is not mounted"


def is_denied(rel_path: str) -> bool:
    """Report whether a file's text must never leave this machine."""
    name = posixpath.basename(rel_path)
    return any(fnmatch.fnmatch(name, pattern) for pattern in CONTENT_DENIED_NAMES)


def read(project: str, rel_path: str, limit: int = 0) -> tuple[str | None, str]:
    """Return a file's text, or None and the reason there is none."""
    if is_denied(rel_path):
        return None, DENIED
    tree = trees.of(project)
    if not tree.available():
        return None, UNMOUNTED
    if not tree.contains(rel_path):
        return None, ESCAPES
    content, reason = tree.read(rel_path)
    if content is None:
        return None, reason
    return (content[:limit] if limit > 0 else content), ""
