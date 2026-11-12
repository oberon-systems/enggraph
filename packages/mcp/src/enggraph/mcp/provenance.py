"""Where each text of a tool answer comes from, so a reader knows what to act on.

An answer mixes what somebody wrote for the agent - a plan, a memory, an
issue - with text copied out of an indexed tree, which whoever wrote the tree
controls. `provenance` on an object names the origin of every text in it and
below it, until an object further in names its own; `summary_provenance`
names the origin of that object's summary when it is another one.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# Copied out of an indexed tree: file text, lines, names, paths.
TREE = "tree"
# Written by the summarizing model from text of a tree.
MODEL = "model"
# Written through this server or the dashboard: plans, memories, issues, skills.
RECORD = "record"
# Stored before origins were kept, or with none: not to be acted on unasked.
UNKNOWN = "unknown"

KEY = "provenance"
SUMMARY_KEY = "summary_provenance"
SOURCE_KEY = "summary_source"
# What nodes.metadata ->> 'summary_source' holds, and what each one means here.
SOURCES = {"auto": TREE, "llm": MODEL, "manual": RECORD}
# Inside an answer about a tree, what sits under these keys is records.
RECORD_SUBTREES = frozenset({"knowledge"})
TREE_TOOLS = frozenset(
    {
        "describe_project",
        "find_callees",
        "find_callers",
        "find_definition",
        "find_implementations",
        "find_linked_name",
        "find_references",
        "find_tests",
        "get_code_graph_neighbors",
        "get_context",
        "get_file_hash",
        "get_node_summary",
        "get_overview",
        "get_project_links",
        "impact_analysis",
        "list_indexed_files",
        "search_code",
        "search_code_nodes",
        "search_text",
        "shortest_path",
        "trace",
    }
)
# The tool whose answer has always carried the stored source beside a summary.
SOURCE_KEPT = frozenset({"get_overview"})

# A tool this table does not know answers as a tree: text nobody is to act on.
ORIGIN: ContextVar[str] = ContextVar("origin", default=TREE)
KEEP_SOURCE: ContextVar[bool] = ContextVar("keep_source", default=False)


def origin_of(tool: str) -> str:
    """Return where the text of a tool's answer comes from, unless it says otherwise."""
    return TREE if tool in TREE_TOOLS else RECORD


def summary_origin(stored: object) -> str:
    """Return the origin of a summary from what was stored with it."""
    return SOURCES.get(stored, UNKNOWN) if isinstance(stored, str) else UNKNOWN


def stamp(value: Any, origin: str | None = None, stated: str | None = None) -> Any:  # noqa: ANN401
    """Return an answer with the origin of its text named wherever it changes."""
    origin = ORIGIN.get() if origin is None else origin
    if isinstance(value, list):
        return [stamp(one, origin, stated) for one in value]
    if not isinstance(value, dict):
        return value
    own = value.get(KEY) if isinstance(value.get(KEY), str) else origin
    marked: dict[str, Any] = {}
    if own != stated:
        marked[KEY] = own
    for key, inner in value.items():
        if key == KEY or (key == SOURCE_KEY and not KEEP_SOURCE.get()):
            continue
        below = RECORD if key in RECORD_SUBTREES else own
        marked[key] = stamp(inner, below, own)
        if key == "summary" and inner and SOURCE_KEY in value:
            told = summary_origin(value[SOURCE_KEY])
            if told != own:
                marked[SUMMARY_KEY] = told
    return marked
