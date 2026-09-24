"""The text a model is shown about one node, whatever kind of node it is.

A file and an entity are read through `sources`, the one seam onto the tree;
a directory is read from the graph alone, as its children's summaries.
"""

from __future__ import annotations

from typing import NamedTuple

from psycopg2.extensions import cursor as Cursor

from enggraph import sources
from enggraph.config import ENTITY_SEPARATOR
from enggraph.hierarchy import DIRECTORY, EDGE_SOURCE, display_name
from enggraph.storage import entity_lines

FILE = "file"
ENTITY = "entity"
KINDS = (FILE, DIRECTORY, ENTITY)
EMPTY = "nothing to describe"
EMPTY_FILE = "the file is empty"
NOT_ENTITY = "the entity is no longer in the graph"
# Claim order: files, then directories deepest first, then entities.
FILE_RANK = 0
DIRECTORY_RANK = 1000
ENTITY_RANK = 2000


class Node(NamedTuple):
    """One node a summary task is about."""

    node_id: str
    kind: str
    file_path: str


def entity_name(node_id: str) -> str:
    """Return the declared name inside an entity id, without its line suffix."""
    name = node_id.split(ENTITY_SEPARATOR, 1)[-1]
    return name.rsplit("@L", 1)[0] if "@L" in name else name


def label(node: Node) -> str:
    """Return what a summary of this node must say more than."""
    if node.kind == DIRECTORY:
        return node.node_id
    if node.kind == ENTITY:
        return entity_name(node.node_id)
    return node.file_path


def subject(node: Node) -> str:
    """Return the line naming the node at the head of the prompt."""
    if node.kind == DIRECTORY:
        return f"Directory: {node.node_id}"
    if node.kind == ENTITY:
        return f"Symbol: {entity_name(node.node_id)} in {node.file_path}"
    return f"File: {node.file_path}"


def directory_texts(
    cursor: Cursor, project: str, limit: int, only: str | None = None
) -> dict[str, str]:
    """Return each directory's listing of its children and their summaries."""
    cursor.execute(
        """
        SELECT e.source_id, n.id, COALESCE(n.summary, '')
          FROM graph_edges AS e
          JOIN graph_nodes AS n
            ON n.project = e.project AND n.id = e.target_id
         WHERE e.project = %s AND e.metadata ->> 'source' = %s
           AND (%s::text IS NULL OR e.source_id = %s)
         ORDER BY e.source_id, (n.type <> %s), n.id;
        """,
        (project, EDGE_SOURCE, only, only, DIRECTORY),
    )
    lines: dict[str, list[str]] = {}
    for directory, child, summary in cursor.fetchall():
        entry = f"- {display_name(str(child))}"
        lines.setdefault(str(directory), []).append(
            f"{entry}: {summary}" if summary else entry
        )
    return {
        directory: "\n".join(entries)[:limit] if limit > 0 else "\n".join(entries)
        for directory, entries in lines.items()
    }


def directory_text(cursor: Cursor, project: str, node_id: str, limit: int) -> str:
    """Return one directory's listing, as `directory_texts` builds it."""
    return directory_texts(cursor, project, limit, node_id).get(node_id, "")


def entity_text(
    cursor: Cursor, project: str, node: Node, limit: int
) -> tuple[str | None, str]:
    """Return the lines of an entity, from its start to the next entity's."""
    starts = entity_lines(cursor, project, node.file_path)
    ids = [node_id for node_id, _ in starts]
    if node.node_id not in ids:
        return None, NOT_ENTITY
    content, reason = sources.read(project, node.file_path)
    if content is None:
        return None, reason
    at = ids.index(node.node_id)
    first = starts[at][1]
    following = [line for _, line in starts[at + 1 :] if line > first]
    lines = content.splitlines()
    end = following[0] - 1 if following else len(lines)
    text = "\n".join(lines[first - 1 : end])
    return (text[:limit] if limit > 0 else text), ""


def read(cursor: Cursor, project: str, node: Node, limit: int) -> tuple[str, str]:
    """Return the text of one node, or an empty text and the reason."""
    if node.kind == DIRECTORY:
        text = directory_text(cursor, project, node.node_id, limit)
        return (text, "") if text.strip() else ("", EMPTY)
    if node.kind == ENTITY:
        content, reason = entity_text(cursor, project, node, limit)
    else:
        content, reason = sources.read(project, node.file_path, limit)
    if content and content.strip():
        return content, ""
    return "", reason or (EMPTY_FILE if node.kind == FILE else EMPTY)
