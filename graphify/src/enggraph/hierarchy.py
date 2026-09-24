"""Directory nodes above the files, and the summaries they start with.

Built from the file node ids already in the graph, never from a tree on disk,
so the ladder repository -> directory -> file needs no mount to exist.
"""

from __future__ import annotations

import posixpath

from psycopg2.extensions import cursor as Cursor

from enggraph.config import (
    MAX_NAME_LENGTH,
    MAX_SUMMARY_LENGTH,
    SUMMARY_ENTITY_LIMIT,
)
from enggraph.identifiers import truncate

DIRECTORY = "directory"
ROOT_ID = "./"
EDGE_SOURCE = "hierarchy"
LANDING_STEMS = ("readme", "__init__", "index")


def parent_of(node_id: str) -> str:
    """Return the directory id holding a file or a directory id."""
    parent = posixpath.dirname(node_id.rstrip("/"))
    return f"{parent}/" if parent else ROOT_ID


def depth_of(node_id: str) -> int:
    """Count the path segments of a directory id; the root is zero."""
    return 0 if node_id == ROOT_ID else node_id.rstrip("/").count("/") + 1


def display_name(node_id: str) -> str:
    """Name a child the way a listing reads it: directories end in a slash."""
    base = posixpath.basename(node_id.rstrip("/"))
    return f"{base}/" if node_id.endswith("/") else base


def plan_tree(file_ids: list[str]) -> dict[str, list[str]]:
    """Map every directory id to its children, directories first, sorted."""
    tree: dict[str, set[str]] = {}
    for file_id in file_ids:
        child = file_id
        while True:
            parent = parent_of(child)
            tree.setdefault(parent, set()).add(child)
            if parent == ROOT_ID:
                break
            child = parent
    return {
        directory: sorted(children, key=lambda c: (not c.endswith("/"), c))
        for directory, children in tree.items()
    }


def landing_summary(children: list[str], summaries: dict[str, str]) -> str:
    """Return the summary of the README, __init__ or index file of a directory."""
    for stem in LANDING_STEMS:
        for child in children:
            name = display_name(child).lower()
            if not child.endswith("/") and name.split(".", 1)[0] == stem:
                summary = summaries.get(child) or ""
                if summary:
                    return summary
    return ""


def listing_summary(children: list[str]) -> str:
    """Say how much a directory holds and name the first of it."""
    directories = sum(1 for child in children if child.endswith("/"))
    files = len(children) - directories
    counts = []
    if files:
        counts.append(f"{files} file{'' if files == 1 else 's'}")
    if directories:
        counts.append(f"{directories} director{'y' if directories == 1 else 'ies'}")
    listed = ", ".join(display_name(child) for child in children[:SUMMARY_ENTITY_LIMIT])
    if len(children) > SUMMARY_ENTITY_LIMIT:
        listed += f", +{len(children) - SUMMARY_ENTITY_LIMIT} more"
    return truncate(f"Holds {' and '.join(counts)}: {listed}", MAX_SUMMARY_LENGTH)


def auto_summary(
    directory: str,
    children: list[str],
    summaries: dict[str, str],
    description: str = "",
) -> str:
    """Summarize a directory without a model."""
    if directory == ROOT_ID and description:
        return truncate(description, MAX_SUMMARY_LENGTH)
    return landing_summary(children, summaries) or listing_summary(children)


def rebuild(cursor: Cursor, project: str) -> int:
    """Rewrite a project's directory nodes and their edges. Returns the count."""
    cursor.execute(
        """
        SELECT id, COALESCE(summary, '') FROM graph_nodes
         WHERE project = %s AND type = 'file' AND file_path IS NOT NULL;
        """,
        (project,),
    )
    summaries = {str(node_id): str(summary) for node_id, summary in cursor.fetchall()}
    tree = plan_tree(list(summaries))

    cursor.execute(
        "SELECT COALESCE(description, '') FROM projects WHERE name = %s;",
        (project,),
    )
    row = cursor.fetchone()
    description = str(row[0]) if row else ""

    cursor.execute(
        "DELETE FROM graph_nodes WHERE project = %s AND type = %s "
        "AND NOT (id = ANY(%s));",
        (project, DIRECTORY, list(tree)),
    )
    cursor.execute(
        "DELETE FROM graph_edges WHERE project = %s AND metadata ->> 'source' = %s;",
        (project, EDGE_SOURCE),
    )
    if not tree:
        return 0

    directories = list(tree)
    cursor.execute(
        """
        INSERT INTO graph_nodes (project, id, name, type, summary, metadata)
        SELECT %s, u.id, u.name, 'directory', u.summary,
               JSONB_BUILD_OBJECT('summary_source', 'auto', 'source', 'hierarchy')
          FROM UNNEST(%s::text[], %s::text[], %s::text[]) AS u (id, name, summary)
        ON CONFLICT (project, id) DO UPDATE SET
            name = EXCLUDED.name,
            type = EXCLUDED.type,
            summary = CASE
                WHEN COALESCE(graph_nodes.metadata ->> 'summary_source', 'auto')
                     = 'auto'
                THEN EXCLUDED.summary
                ELSE graph_nodes.summary
            END;
        """,
        (
            project,
            directories,
            [
                truncate(
                    project if directory == ROOT_ID else display_name(directory),
                    MAX_NAME_LENGTH,
                )
                for directory in directories
            ],
            [
                auto_summary(directory, tree[directory], summaries, description)
                for directory in directories
            ],
        ),
    )
    pairs = [(parent, child) for parent in directories for child in tree[parent]]
    cursor.execute(
        """
        INSERT INTO graph_edges (
            project, source_id, target_id, relation_type, metadata
        )
        SELECT %s, u.source_id, u.target_id, 'contains',
               '{"source": "hierarchy"}'::JSONB
          FROM UNNEST(%s::text[], %s::text[]) AS u (source_id, target_id)
        ON CONFLICT DO NOTHING;
        """,
        (project, [pair[0] for pair in pairs], [pair[1] for pair in pairs]),
    )
    return len(directories)
