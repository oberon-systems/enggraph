"""Every statement the indexer sends to PostgreSQL.

One database holds several projects, so every statement here is scoped to one.
The scope is passed in rather than read from the configuration: the caller
already knows which tree it is walking, and a module level default is exactly
what would let one project's re-index quietly delete another's rows.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath

import psycopg2
from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor

from enggraph.config import (
    BUILTIN_PROJECT_TYPES,
    DEFAULT_PROJECT_TYPE,
    MAX_NAME_LENGTH,
    MAX_NODE_ID_LENGTH,
    MAX_TYPE_LENGTH,
    ORGANIZATION_PROJECT_TYPE,
    SOURCE_NATIVE,
)
from enggraph.identifiers import entity_node_id, project_name, truncate

LOG = logging.getLogger(__name__)

# The built-in projects holding what an agent wrote about a codebase. A plan
# carries the project it is about in its metadata alone; a memory and a
# suggestion carry it in their node id as well, as `<about>/<slug>`.
MEMORY_PROJECT = "_memory"
PLANS_PROJECT = "_plans"
SUGGESTIONS_PROJECT = "_suggestions"
RECORD_PROJECTS = (MEMORY_PROJECT, PLANS_PROJECT, SUGGESTIONS_PROJECT)
SCOPED_RECORD_PROJECTS = (MEMORY_PROJECT, SUGGESTIONS_PROJECT)


def registered_root(project: str) -> str:
    """Return the root a project reading no directory is recorded under.

    projects.root_path is NOT NULL UNIQUE, so a project with nothing to read
    still needs one, and where it was registered is the honest answer.
    """
    return f"registered://{project}"


def rescope_record(node_id: str, about: str) -> str:
    """Re-scope the id of a memory or a suggestion to another project.

    The same rule the SQL uses, so the collision check and the update that
    follows it cannot disagree about the id a record is about to take.
    """
    tail = node_id.split("/", 1)[1] if "/" in node_id else node_id
    return f"{about}/{tail}"


def get_db_url() -> str:
    """Read DATABASE_URL, or say which variable is missing."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set")
    return db_url


def get_db_connection() -> Connection:
    """Open a connection using DATABASE_URL."""
    return psycopg2.connect(get_db_url())


def project_root(cursor: Cursor, project: str) -> str | None:
    """Return the host directory a project reads, or None when it has no row."""
    cursor.execute("SELECT root_path FROM projects WHERE name = %s;", (project,))
    row = cursor.fetchone()
    return str(row[0]) if row is not None else None


def path_owner(cursor: Cursor, root_path: str) -> str | None:
    """Return the project a host directory already belongs to, if any."""
    cursor.execute("SELECT name FROM projects WHERE root_path = %s;", (root_path,))
    row = cursor.fetchone()
    return str(row[0]) if row is not None else None


def stored_type(cursor: Cursor, project: str) -> str | None:
    """Return what kind of project this is, or None when there is no row.

    Named for the column rather than for the argument every other function
    here calls `project_type`, which is the type a caller is asking to store.
    """
    cursor.execute("SELECT type FROM projects WHERE name = %s;", (project,))
    row = cursor.fetchone()
    return str(row[0]) if row is not None else None


def project_rows(cursor: Cursor, names: list[str]) -> dict[str, dict[str, object]]:
    """Read what a listing says about each of these projects.

    One statement rather than one per name: an organization listing its
    members asks this about all of them at once. `stale_seconds` is measured
    by the database, so a clock elsewhere cannot make an index run look older
    or fresher than it is.
    """
    if not names:
        return {}
    cursor.execute(
        """
        SELECT
            name,
            description,
            root_path,
            indexed_at,
            EXTRACT(EPOCH FROM (NOW() - indexed_at))
          FROM projects WHERE name = ANY(%s);
        """,
        (names,),
    )
    return {
        str(name): {
            "description": None if text is None else str(text),
            "root_path": str(root_path),
            "indexed_at": indexed_at,
            "stale_seconds": None if stale is None else int(stale),
        }
        for name, text, root_path, indexed_at, stale in cursor.fetchall()
    }


def list_owned(cursor: Cursor, organization: str) -> list[str]:
    """Read the projects that were moved into an organization, not added."""
    cursor.execute(
        """
        SELECT project FROM project_members
         WHERE organization = %s AND owned ORDER BY created_at, project;
        """,
        (organization,),
    )
    return [str(name) for (name,) in cursor.fetchall()]


def list_members(cursor: Cursor, organization: str) -> list[str]:
    """Read the projects an organization holds, in the order they joined."""
    cursor.execute(
        """
        SELECT project FROM project_members
         WHERE organization = %s ORDER BY created_at, project;
        """,
        (organization,),
    )
    return [str(name) for (name,) in cursor.fetchall()]


def list_memberships(cursor: Cursor, project: str) -> list[str]:
    """Read the organizations a project is part of."""
    cursor.execute(
        """
        SELECT organization FROM project_members
         WHERE project = %s ORDER BY created_at, organization;
        """,
        (project,),
    )
    return [str(name) for (name,) in cursor.fetchall()]


def owner_of(cursor: Cursor, project: str) -> str | None:
    """Return the organization a project was moved into, if it was.

    A project has at most one - the partial unique index says so - and it is
    what decides where the project is listed: under that organization, rather
    than beside the projects nothing holds.
    """
    cursor.execute(
        "SELECT organization FROM project_members WHERE project = %s AND owned;",
        (project,),
    )
    row = cursor.fetchone()
    return str(row[0]) if row is not None else None


def require_unheld(cursor: Cursor, project: str, what: str) -> None:
    """Refuse to dissolve a project some organization still lists.

    Membership is a reference rather than ownership, so nothing here follows
    it quietly: an organization pointing at a name that stopped existing would
    answer a search with a hole, and which organizations lose the project is a
    decision rather than a consequence.
    """
    holders = list_memberships(cursor, project)
    if holders:
        raise RuntimeError(
            f"project {project!r} is part of "
            f"{', '.join(repr(name) for name in holders)}; take it out of "
            f"{'those organizations' if len(holders) > 1 else 'that organization'} "
            f"before {what}"
        )


def add_member(
    cursor: Cursor, organization: str, project: str, owned: bool = False
) -> None:
    """Add one project to an organization, leaving the project untouched.

    Nothing moves and nothing is copied whichever way this is called: the
    member keeps its name, its tree, its mount, its node ids and its graph.

    `owned` is the difference between a project added to an organization and
    one moved into it. Added, it is a reference: the project stays a project of
    its own, listed beside the others, and belongs to as many organizations as
    are relevant to it. Moved, the organization is where it lives: it is listed
    there instead. Which listing it appears in is the whole of it.
    """
    if organization == project:
        raise RuntimeError(f"project {organization!r} cannot be part of itself")
    types: dict[str, str] = {}
    for name in (organization, project):
        cursor.execute("SELECT type FROM projects WHERE name = %s;", (name,))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no project named {name!r}")
        types[name] = str(row[0])
    if types[organization] != ORGANIZATION_PROJECT_TYPE:
        raise RuntimeError(
            f"project {organization!r} is a {types[organization]}, not an "
            f"{ORGANIZATION_PROJECT_TYPE}; only an "
            f"{ORGANIZATION_PROJECT_TYPE} holds other projects"
        )
    if types[project] in BUILTIN_PROJECT_TYPES:
        raise RuntimeError(
            f"project {project!r} holds agent {types[project]}, not an "
            "indexed tree; there is nothing to search in it"
        )
    if organization in list_members(cursor, project):
        raise RuntimeError(
            f"project {project!r} already holds {organization!r}; one of the "
            "two has to be the organization"
        )
    owner = owner_of(cursor, project)
    if owner is not None and owner != organization:
        raise RuntimeError(
            f"project {project!r} was moved into {owner!r} and is listed "
            f"there; take it out of {owner!r} first, and it is a project of "
            "its own again"
        )
    cursor.execute(
        """
        INSERT INTO project_members (organization, project, owned)
        VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;
        """,
        (organization, project, owned),
    )


def drop_member(cursor: Cursor, organization: str, project: str) -> None:
    """Take one project out of an organization, leaving the project itself."""
    cursor.execute(
        """
        DELETE FROM project_members
         WHERE organization = %s AND project = %s;
        """,
        (organization, project),
    )


def set_memberships(cursor: Cursor, project: str, organizations: list[str]) -> None:
    """Move a project into these organizations, out of the ones not named.

    Adding is a project joining one more organization, as a reference, and it
    stays a project of its own in the projects list. This is it moving in: the
    organization becomes where it lives and where it is listed. Both are rows
    and nothing else - the tree, the mount, the node ids and the graph of a
    project are not what its membership is about, and neither is indexed
    again.

    An organization already holding it is not let go of here. Leaving one is a
    decision about that organization - a search stops reaching the project, and
    the settings it inherited stop applying - so it is taken by taking the
    project out, on either page, and not as a side effect of joining another.
    """
    wanted = list(dict.fromkeys(organizations))
    held = list_memberships(cursor, project)
    leaving = [name for name in held if name not in wanted]
    if leaving:
        raise RuntimeError(
            f"project {project!r} is part of "
            f"{', '.join(repr(name) for name in leaving)}; take it out of "
            f"{'those' if len(leaving) > 1 else 'that one'} first, or add it "
            "to another organization as well"
        )
    for name in wanted:
        if name not in held:
            add_member(cursor, name, project, owned=True)


def read_records_about(cursor: Cursor, project: str) -> list[tuple[str, str]]:
    """Read what an agent wrote about a project, as (holder, node id)."""
    cursor.execute(
        """
        SELECT project, id FROM graph_nodes
         WHERE project = ANY(%s) AND metadata ->> 'about' = %s;
        """,
        (list(RECORD_PROJECTS), project),
    )
    return [(str(holder), str(node)) for holder, node in cursor.fetchall()]


def count_records(records: list[tuple[str, str]]) -> dict[str, int]:
    """Count records by the built-in project holding them."""
    return {
        "memories": sum(1 for holder, _ in records if holder == MEMORY_PROJECT),
        "plans": sum(1 for holder, _ in records if holder == PLANS_PROJECT),
        "suggestions": sum(1 for holder, _ in records if holder == SUGGESTIONS_PROJECT),
    }


def check_record_scope(
    cursor: Cursor, records: list[tuple[str, str]], donor: str, target: str
) -> None:
    """Refuse a re-scope that would overwrite a record the target already holds.

    A memory and a suggestion are keyed `<about>/<slug>`, so following a name
    means taking a new id, and that id may be someone else's.
    """
    taken: list[str] = []
    for holder in SCOPED_RECORD_PROJECTS:
        wanted = [
            rescope_record(node, target) for name, node in records if name == holder
        ]
        if not wanted:
            continue
        cursor.execute(
            "SELECT id FROM graph_nodes WHERE project = %s AND id = ANY(%s);",
            (holder, wanted),
        )
        taken.extend(str(row[0]) for row in cursor.fetchall())
    if taken:
        raise RuntimeError(
            f"{', '.join(sorted(taken))} already exists under {target!r}; drop "
            f"or rename it before moving {donor!r} into it"
        )


def rename_project(cursor: Cursor, project: str, wanted: str) -> dict[str, object]:
    """Give a project another name, and move every row that names it.

    The name is the project's identity: it keys the graph, addresses the MCP
    server at `/mcp/<name>`, names the mount at `/code/<name>` and tags every
    record an agent wrote about the project. Nothing is re-read and nothing is
    re-derived - node ids are relative to a directory, not to the project, so
    the graph is the same graph under a new key.

    The foreign keys carry the graph, the settings and the memberships
    (migration 0018). The two things no key reaches are done here:
    the index runs, which reference nothing, and the records, which carry the
    name in `metadata ->> 'about'` and in their own ids.

    The mount does not follow. It is a file on the host and the services hold
    the ones they started with, so `make mounts` finishes the rename, which is
    what the reply says.
    """
    new_name = project_name(wanted, "")
    if new_name == project:
        raise RuntimeError(f"project {project!r} is already called that")
    stored = stored_type(cursor, project)
    if stored is None:
        raise RuntimeError(f"no project named {project!r}")
    if stored in BUILTIN_PROJECT_TYPES:
        raise RuntimeError(
            f"project {project!r} holds agent {stored}, not an indexed tree; "
            "its name is what the tools address it by and is not a choice"
        )
    cursor.execute("SELECT 1 FROM projects WHERE name = %s;", (new_name,))
    if cursor.fetchone() is not None:
        raise RuntimeError(
            f"project {new_name!r} already exists; a name belongs to one "
            "project, so drop that one or pick another name"
        )
    cursor.execute(
        "SELECT 1 FROM index_jobs WHERE project = %s AND status = 'running';",
        (project,),
    )
    if cursor.fetchone() is not None:
        raise RuntimeError(
            f"project {project!r} is being indexed; wait for that run to "
            "finish, because it writes rows under the name being changed"
        )

    # A record about a name no project row carries is legitimate - a plan
    # outlives the codebase it names - so the new name may be spoken for even
    # though no project holds it.
    records = read_records_about(cursor, project)
    check_record_scope(cursor, records, project, new_name)

    cursor.execute(
        "UPDATE projects SET name = %s WHERE name = %s;", (new_name, project)
    )
    # The cascade moves every edge and embedding whose node it can find. A row
    # whose node is gone is not one of those: `graph_edges` holds some, which
    # is why migration 0018 could not re-validate that key, and left behind
    # they would name a project that no longer exists. They are swept rather
    # than deleted - what to do about them is a decision about data.
    for table in ("graph_edges", "code_embeddings"):
        cursor.execute(
            f"UPDATE {table} SET project = %s WHERE project = %s;",  # noqa: S608
            (new_name, project),
        )
    # No foreign key reaches this table, so the runs would be left behind under
    # a name that stopped existing.
    cursor.execute(
        "UPDATE index_jobs SET project = %s WHERE project = %s;",
        (new_name, project),
    )
    cursor.execute(
        """
        UPDATE graph_nodes
           SET metadata = jsonb_set(metadata, '{about}', to_jsonb(%s::text))
         WHERE project = %s AND metadata ->> 'about' = %s;
        """,
        (new_name, PLANS_PROJECT, project),
    )
    cursor.execute(
        """
        UPDATE graph_nodes
           SET id = %s || substring(id from position('/' in id) + 1),
               metadata = jsonb_set(metadata, '{about}', to_jsonb(%s::text))
         WHERE project = ANY(%s) AND metadata ->> 'about' = %s;
        """,
        (f"{new_name}/", new_name, list(SCOPED_RECORD_PROJECTS), project),
    )
    # A project registered before it read anything carries the synthetic root
    # built from the name that just changed.
    cursor.execute(
        "UPDATE projects SET root_path = %s WHERE name = %s AND root_path = %s;",
        (registered_root(new_name), new_name, registered_root(project)),
    )
    return {
        "project": new_name,
        "was": project,
        "root_path": project_root(cursor, new_name),
        **count_records(records),
    }


def read_settings(cursor: Cursor, project: str) -> tuple[str | None, str | None]:
    """Read one settings row as (ctxkeep, ctxignore), without any fallback.

    The precedence between the levels is `enggraph.selection`'s business, so
    this answers about the one row it was asked for and nothing else. A missing
    row and a row holding two NULLs are the same answer on purpose: both mean
    this level says nothing about the selection.
    """
    cursor.execute(
        "SELECT ctxkeep, ctxignore FROM project_settings WHERE project = %s;",
        (project,),
    )
    row = cursor.fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def write_settings(
    cursor: Cursor, project: str, ctxkeep: str | None, ctxignore: str | None
) -> None:
    """Store the selection documents for one level, verbatim.

    Both are written every time, NULL included: clearing one document is how a
    level stops speaking for it and lets the level above answer instead.
    """
    cursor.execute(
        """
        INSERT INTO project_settings (project, ctxkeep, ctxignore)
        VALUES (%s, %s, %s)
        ON CONFLICT (project) DO UPDATE SET
            ctxkeep = EXCLUDED.ctxkeep,
            ctxignore = EXCLUDED.ctxignore,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (project, ctxkeep, ctxignore),
    )


def clear_settings(cursor: Cursor, project: str) -> None:
    """Drop one settings row, so the level above it takes over again."""
    cursor.execute("DELETE FROM project_settings WHERE project = %s;", (project,))


def read_settings_json(cursor: Cursor, project: str) -> dict:
    """Read the settings object of one level, without any fallback.

    The two selection documents are columns of their own; everything else a
    level says - the indexing schedule today - is one JSONB object, so a new
    knob is a key rather than a migration.
    """
    cursor.execute(
        "SELECT settings FROM project_settings WHERE project = %s;",
        (project,),
    )
    row = cursor.fetchone()
    if row is None or not isinstance(row[0], dict):
        return {}
    return row[0]


def write_settings_json(
    cursor: Cursor, project: str, key: str, value: dict | None
) -> None:
    """Store or drop one key of a level's settings, leaving the rest alone.

    The row is created when it does not exist: a project may say when it is
    indexed while saying nothing about what is indexed. `None` removes the key
    rather than storing an empty object, because that is what sends the
    question back to the level above.
    """
    if value is None:
        cursor.execute(
            """
            UPDATE project_settings
               SET settings = settings - %s, updated_at = CURRENT_TIMESTAMP
             WHERE project = %s;
            """,
            (key, project),
        )
        return
    cursor.execute(
        """
        INSERT INTO project_settings (project, settings)
        VALUES (%s, %s::jsonb)
        ON CONFLICT (project) DO UPDATE SET
            settings = project_settings.settings || EXCLUDED.settings,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (project, json.dumps({key: value})),
    )


def has_settings(cursor: Cursor, project: str) -> bool:
    """Report whether a project holds a settings row of its own.

    Onboarding writes the generated pair only into a project that has none,
    which is the same rule that kept it from replacing a file already in a
    tree.
    """
    cursor.execute(
        "SELECT 1 FROM project_settings WHERE project = %s LIMIT 1;",
        (project,),
    )
    return cursor.fetchone() is not None


def set_selection_origin(cursor: Cursor, project: str, keep: str, ignore: str) -> None:
    """Record where an index run read a project's selection from.

    The dashboard holds no mount and cannot look at a tree, so the run that
    did the looking is what reports it.
    """
    cursor.execute(
        """
        UPDATE projects
           SET keep_source = %s, ignore_source = %s
         WHERE name = %s;
        """,
        (keep, ignore, project),
    )


def check_project_identity(cursor: Cursor, project: str, root_path: str) -> None:
    """Refuse a name/path pairing that would merge or orphan a graph.

    Both directions are checked, and neither is repaired silently. A name
    pointing at a new path means two checkouts share a directory name, and
    letting the second one through would merge two unrelated codebases into
    one graph. A path arriving under a new name means a rename, which is
    legitimate but has to move the existing rows rather than orphan them, so
    it is refused here rather than half done.
    """
    cursor.execute("SELECT type FROM projects WHERE name = %s;", (project,))
    row = cursor.fetchone()
    # A built-in project holds records written through the MCP tools; there
    # is no tree behind it, and an index run would prune every one of them.
    if row is not None and row[0] in BUILTIN_PROJECT_TYPES:
        raise RuntimeError(
            f"project {project!r} holds agent {row[0]}, not an indexed tree; "
            f"indexing into it would delete every record it holds"
        )

    stored_root = project_root(cursor, project)
    if stored_root is not None and stored_root != root_path:
        raise RuntimeError(
            f"project {project!r} is already indexed from {stored_root!r}; "
            f"pass PROJECT_NAME to index {root_path!r} under another name"
        )

    owner = path_owner(cursor, root_path)
    if owner is not None and owner != project:
        raise RuntimeError(
            f"{root_path!r} is already indexed as {owner!r}; "
            f"rename it in the projects table before indexing it as {project!r}"
        )


def ensure_project(
    cursor: Cursor, project: str, root_path: str, project_type: str | None = None
) -> None:
    """Register the project being indexed, or refresh when it was.

    `project_type` categorises the project for the cross-project MCP search.
    None means "leave whatever is stored alone", so a plain re-index does not
    demote a project that was registered as something other than the default.
    """
    check_project_identity(cursor, project, root_path)
    # A project registered before it read anything carries the synthetic root
    # and has no tree to walk. Registering `root_path` as its tree here is
    # exactly what its owner avoided by onboarding it empty.
    if project_root(cursor, project) == registered_root(project):
        raise RuntimeError(
            f"project {project!r} reads no directory yet; give it one on its "
            "own page in the dashboard before indexing it"
        )
    cursor.execute(
        """
        INSERT INTO projects (name, root_path, indexed_at, type)
        VALUES (%s, %s, CURRENT_TIMESTAMP, COALESCE(%s, %s))
        ON CONFLICT (name) DO UPDATE SET
            indexed_at = CURRENT_TIMESTAMP,
            type = COALESCE(%s, projects.type);
        """,
        (project, root_path, project_type, DEFAULT_PROJECT_TYPE, project_type),
    )


def register_project(
    cursor: Cursor, project: str, root_path: str, project_type: str | None = None
) -> None:
    """Record a tree as a project without claiming it has been indexed.

    Onboarding writes the row so the tree is listed, mounted and offered an
    index run; the graph itself comes later. `indexed_at` is therefore left
    alone in both branches - NULL on the insert, untouched on the update - so
    a project already indexed keeps its freshness when it is onboarded again.

    `root_path` may be the synthetic `registered://<name>`, which is a project
    onboarded before it has a tree to read - an organization, or one waiting
    for the path it will be given.
    """
    check_project_identity(cursor, project, root_path)
    cursor.execute(
        """
        INSERT INTO projects (name, root_path, indexed_at, type)
        VALUES (%s, %s, NULL, COALESCE(%s, %s))
        ON CONFLICT (name) DO UPDATE SET
            type = COALESCE(%s, projects.type);
        """,
        (project, root_path, project_type, DEFAULT_PROJECT_TYPE, project_type),
    )


def list_projects(cursor: Cursor) -> list[tuple[str, str, str, int]]:
    """Read every indexed project with its root, its type and its node count."""
    cursor.execute(
        """
        SELECT p.name, p.root_path, p.type, COUNT(n.id)
          FROM projects AS p
          LEFT JOIN graph_nodes AS n ON n.project = p.name
         GROUP BY p.name, p.root_path, p.type
         ORDER BY p.name;
        """
    )
    return cursor.fetchall()


def list_mountable_projects(cursor: Cursor) -> list[tuple[str, str]]:
    """Read every project standing for a host directory, as (name, path).

    A project registered before it has a tree carries the synthetic
    `registered://<name>` root, and an organization keeps it for good: neither
    is a directory anything can mount or walk.
    """
    return [
        (name, root_path)
        for name, root_path in list_indexable_projects(cursor)
        if root_path != registered_root(name)
    ]


def list_indexable_projects(cursor: Cursor) -> list[tuple[str, str]]:
    """Read every project that stands for a tree, as (name, host path).

    The built-in projects hold records written through the MCP tools, and an
    index run over one of them would prune every record it found, so they are
    not offered to anything that indexes on its own.
    """
    cursor.execute(
        """
        SELECT name, root_path FROM projects
         WHERE NOT (type = ANY(%s)) ORDER BY name;
        """,
        (list(BUILTIN_PROJECT_TYPES),),
    )
    return [(str(name), str(root_path)) for name, root_path in cursor.fetchall()]


def upsert_file_node(
    cursor: Cursor,
    project: str,
    rel_path: str,
    summary: str,
    source: str = SOURCE_NATIVE,
) -> None:
    """Insert or refresh the node standing for a file.

    A summary written through the MCP `save_node_summary` tool is marked
    manual in the metadata and survives re-indexing; the generated one does
    not, so it keeps up with the file.

    `source` records which producer found the file. It is merged rather than
    replaced so a node that already carries a manual summary keeps the rest of
    its metadata.
    """
    cursor.execute(
        """
        INSERT INTO graph_nodes (
            project, id, name, type, file_path, summary, metadata
        )
        VALUES (
            %s, %s, %s, 'file', %s, %s,
            JSONB_BUILD_OBJECT('summary_source', 'auto', 'source', %s)
        )
        ON CONFLICT (project, id) DO UPDATE SET
            name = EXCLUDED.name,
            type = 'file',
            file_path = EXCLUDED.file_path,
            metadata = graph_nodes.metadata || JSONB_BUILD_OBJECT('source', %s),
            summary = CASE
                WHEN COALESCE(
                    graph_nodes.metadata ->> 'summary_source', 'auto'
                ) = 'auto'
                THEN EXCLUDED.summary
                ELSE graph_nodes.summary
            END;
        """,
        (
            project,
            truncate(rel_path, MAX_NODE_ID_LENGTH),
            truncate(posixpath.basename(rel_path), MAX_NAME_LENGTH),
            rel_path,
            summary,
            source,
            source,
        ),
    )


def clear_file_artifacts(cursor: Cursor, project: str, rel_path: str) -> None:
    """Drop what a previous run derived from a file.

    Without this an entity or a call that was deleted from the source stays in
    the graph forever, because every write below is an upsert.
    """
    file_id = truncate(rel_path, MAX_NODE_ID_LENGTH)
    cursor.execute(
        """
        DELETE FROM graph_nodes
        WHERE project = %s AND file_path = %s AND type <> 'file';
        """,
        (project, rel_path),
    )
    cursor.execute(
        "DELETE FROM graph_edges WHERE project = %s AND source_id = %s;",
        (project, file_id),
    )


def prune_orphans(cursor: Cursor, project: str) -> int:
    """Delete placeholder nodes nothing points at any more.

    An import or a call that was removed from the source leaves its external
    node behind, and releases before entity ids were file scoped wrote entity
    nodes without a file_path. Both are dead weight in every search result.
    """
    cursor.execute(
        """
        DELETE FROM graph_nodes
        WHERE project = %s AND ((
            type IN ('external_import', 'external_symbol')
            AND NOT EXISTS (
                SELECT 1 FROM graph_edges
                 WHERE project = graph_nodes.project
                   AND target_id = graph_nodes.id
            )
        ) OR (
            file_path IS NULL
            AND type NOT IN ('file', 'external_import', 'external_symbol')
        ));
        """,
        (project,),
    )
    return cursor.rowcount


def prune_missing_files(cursor: Cursor, project: str, known_paths: list[str]) -> int:
    """Delete everything derived from a file that is no longer in the tree.

    A re-index only visits the files it finds, so a file that was renamed or
    deleted is never reached by the per-file cleanup and its nodes outlive it.
    The set of files just discovered is the only thing that knows they are
    gone.
    """
    if not known_paths:
        return 0
    cursor.execute(
        "DELETE FROM graph_nodes WHERE project = %s AND file_path IS NOT NULL "
        "AND NOT (file_path = ANY(%s));",
        (project, known_paths),
    )
    removed = cursor.rowcount
    cursor.execute(
        "DELETE FROM file_hashes WHERE project = %s AND NOT (file_path = ANY(%s));",
        (project, known_paths),
    )
    return removed


def ensure_external_node(
    cursor: Cursor, project: str, node_id: str, node_type: str
) -> None:
    """Create a placeholder node for a target defined outside the project."""
    cursor.execute(
        """
        INSERT INTO graph_nodes (project, id, name, type)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (project, id) DO NOTHING;
        """,
        (project, node_id, truncate(node_id, MAX_NAME_LENGTH), node_type),
    )


def upsert_entity_node(
    cursor: Cursor,
    project: str,
    rel_path: str,
    entity: dict[str, str],
    source: str = SOURCE_NATIVE,
) -> None:
    """Insert or refresh the node standing for something a file declares."""
    cursor.execute(
        """
        INSERT INTO graph_nodes (project, id, name, type, file_path, metadata)
        VALUES (%s, %s, %s, %s, %s, JSONB_BUILD_OBJECT('source', %s))
        ON CONFLICT (project, id) DO UPDATE SET
            name = EXCLUDED.name,
            type = EXCLUDED.type,
            file_path = EXCLUDED.file_path,
            metadata = graph_nodes.metadata || EXCLUDED.metadata;
        """,
        (
            project,
            entity_node_id(rel_path, entity["name"]),
            truncate(entity["name"], MAX_NAME_LENGTH),
            truncate(entity["type"], MAX_TYPE_LENGTH),
            rel_path,
            source,
        ),
    )


def upsert_extracted_node(
    cursor: Cursor,
    project: str,
    node_id: str,
    name: str,
    node_type: str,
    file_path: str | None,
    summary: str,
    metadata: dict[str, object],
) -> None:
    """Insert or refresh a node whose id was chosen by the caller.

    `upsert_entity_node` derives the id from the owning file and the entity
    name, which is enough for our own parsers. graphifyy names a node after
    its label alone, and those labels repeat across a tree, so the caller has
    to build a unique id and pass it in here.
    """
    cursor.execute(
        """
        INSERT INTO graph_nodes (
            project, id, name, type, file_path, summary, metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::JSONB)
        ON CONFLICT (project, id) DO UPDATE SET
            name = EXCLUDED.name,
            type = EXCLUDED.type,
            file_path = EXCLUDED.file_path,
            metadata = graph_nodes.metadata || EXCLUDED.metadata,
            summary = CASE
                WHEN COALESCE(
                    graph_nodes.metadata ->> 'summary_source', 'auto'
                ) = 'auto'
                THEN EXCLUDED.summary
                ELSE graph_nodes.summary
            END;
        """,
        (
            project,
            truncate(node_id, MAX_NODE_ID_LENGTH),
            truncate(name, MAX_NAME_LENGTH),
            truncate(node_type, MAX_TYPE_LENGTH),
            file_path,
            summary,
            json.dumps(metadata),
        ),
    )


def clear_producer_artifacts(cursor: Cursor, project: str, source: str) -> int:
    """Drop what one producer wrote, leaving the other producer's rows alone.

    File nodes are kept: they are the anchor both producers and the MCP
    summary tools address, and re-creating them would drop a manual summary
    that is supposed to outlive re-indexing. Their entities go, because an
    entity deleted from the source has no other way out of the graph.
    """
    cursor.execute(
        """
        DELETE FROM graph_edges
        WHERE project = %s AND metadata ->> 'source' = %s;
        """,
        (project, source),
    )
    cursor.execute(
        """
        DELETE FROM graph_nodes
        WHERE project = %s AND metadata ->> 'source' = %s AND type <> 'file';
        """,
        (project, source),
    )
    return cursor.rowcount


def get_file_hash(cursor: Cursor, project: str, rel_path: str) -> str | None:
    """Retrieve the stored MD5 hash for a file."""
    cursor.execute(
        "SELECT hash FROM file_hashes WHERE project = %s AND file_path = %s;",
        (project, rel_path),
    )
    result = cursor.fetchone()
    return result[0] if result else None


def save_llm_summary(cursor: Cursor, project: str, rel_path: str, summary: str) -> bool:
    """Replace a generated summary with the model's. Returns whether it was.

    A summary written through `save_node_summary` is marked manual and is not
    touched. Everything else is, which is what lets the backfill improve a
    summary a plain index run wrote from the head of the file.
    """
    cursor.execute(
        """
        UPDATE graph_nodes
           SET summary = %s,
               metadata = metadata || '{"summary_source": "llm"}'::JSONB
         WHERE project = %s AND id = %s
           AND COALESCE(metadata ->> 'summary_source', 'auto') <> 'manual';
        """,
        (summary, project, truncate(rel_path, MAX_NODE_ID_LENGTH)),
    )
    return cursor.rowcount > 0


# The skip bitmask on a file node: which queues leave the file alone. A bit is
# set when a queue gives up on the file and cleared by a retry.
SKIP_SUMMARIZE = 1
SKIP_EMBED = 2
SKIP_NAMES = {SKIP_SUMMARIZE: "summarize", SKIP_EMBED: "embed"}


def mark_skip(
    cursor: Cursor, project: str, rel_path: str, bit: int, reason: str
) -> None:
    """Set one queue's skip bit on a file, with the reason it gave up.

    Matched on the path, as both queues select files, rather than on the node
    id, which a project assembled from several trees prefixes with an alias.
    """
    # A savepoint, so a row the database refuses to rewrite costs this mark
    # alone rather than the whole transaction of the queue that asked.
    cursor.execute("SAVEPOINT mark_skip;")
    try:
        cursor.execute(
            """
            UPDATE graph_nodes
               SET metadata = metadata || JSONB_BUILD_OBJECT(
                       'skip', COALESCE((metadata ->> 'skip')::int, 0) | %s,
                       'skip_reason',
                       COALESCE(metadata -> 'skip_reason', '{}'::jsonb)
                           || JSONB_BUILD_OBJECT(%s::text, %s::text)
                   )
             WHERE project = %s AND type = 'file' AND file_path = %s;
            """,
            (bit, SKIP_NAMES[bit], reason, project, rel_path),
        )
    except psycopg2.Error as error:
        cursor.execute("ROLLBACK TO SAVEPOINT mark_skip;")
        LOG.warning(
            "Could not skip %s of %s for %s: %s",
            rel_path,
            project,
            SKIP_NAMES[bit],
            str(error).splitlines()[0] if str(error) else type(error).__name__,
        )
        return
    cursor.execute("RELEASE SAVEPOINT mark_skip;")
    if cursor.rowcount:
        LOG.info(
            "Skipping %s of %s for %s: %s", rel_path, project, SKIP_NAMES[bit], reason
        )
    else:
        LOG.warning(
            "No file node %s in %s to skip for %s", rel_path, project, SKIP_NAMES[bit]
        )


def clear_skip(cursor: Cursor, project: str | None, bit: int) -> int:
    """Clear one queue's skip bit on every file of a project, or of all of them."""
    cursor.execute(
        """
        UPDATE graph_nodes
           SET metadata = metadata || JSONB_BUILD_OBJECT(
                   'skip', (metadata ->> 'skip')::int & ~%s,
                   'skip_reason',
                   COALESCE(metadata -> 'skip_reason', '{}'::jsonb) - %s::text
               )
         WHERE (%s::text IS NULL OR project = %s) AND type = 'file'
           AND (COALESCE((metadata ->> 'skip')::int, 0) & %s) <> 0
        RETURNING id;
        """,
        (bit, SKIP_NAMES[bit], project, project, bit),
    )
    return len(cursor.fetchall())


def list_skipped(cursor: Cursor, project: str, bit: int) -> list[tuple[str, str]]:
    """Return (file path, reason) for every file one queue gave up on."""
    cursor.execute(
        """
        SELECT file_path, COALESCE(metadata -> 'skip_reason' ->> %s, '')
          FROM graph_nodes
         WHERE project = %s AND type = 'file' AND file_path IS NOT NULL
           AND (COALESCE((metadata ->> 'skip')::int, 0) & %s) <> 0
         ORDER BY file_path;
        """,
        (SKIP_NAMES[bit], project, bit),
    )
    return [(str(row[0]), str(row[1])) for row in cursor.fetchall()]


def list_files_without_llm_summary(
    cursor: Cursor, project: str, refresh: bool = False
) -> list[str]:
    """List the file nodes no model has described yet.

    `refresh` widens it to every file node the model may write to, which is
    all of them but the ones a human wrote through `save_node_summary`.
    """
    cursor.execute(
        """
        SELECT file_path FROM graph_nodes
         WHERE project = %s AND type = 'file' AND file_path IS NOT NULL
           AND COALESCE(metadata ->> 'summary_source', 'auto')
               = ANY(CASE WHEN %s THEN ARRAY['auto', 'llm'] ELSE ARRAY['auto'] END)
         ORDER BY file_path;
        """,
        (project, refresh),
    )
    return [row[0] for row in cursor.fetchall()]


def get_cached_summary(cursor: Cursor, project: str, content_hash: str) -> str | None:
    """Retrieve the summary the model wrote for this exact text, if any."""
    cursor.execute(
        """
        SELECT summary FROM summary_cache
        WHERE project = %s AND content_hash = %s;
        """,
        (project, content_hash),
    )
    result = cursor.fetchone()
    return result[0] if result else None


def put_cached_summary(
    cursor: Cursor, project: str, content_hash: str, summary: str
) -> None:
    """Store what the model answered, so the next run reads it instead."""
    cursor.execute(
        """
        INSERT INTO summary_cache (project, content_hash, summary, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (project, content_hash) DO UPDATE SET
            summary = EXCLUDED.summary,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (project, content_hash, summary),
    )


def upsert_file_hash(
    cursor: Cursor, project: str, rel_path: str, file_hash: str
) -> None:
    """Store or update the MD5 hash for a file."""
    cursor.execute(
        """
        INSERT INTO file_hashes (project, file_path, hash, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (project, file_path) DO UPDATE SET
            hash = EXCLUDED.hash,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (project, rel_path, file_hash),
    )


def get_file_entities(
    cursor: Cursor, project: str, rel_path: str
) -> list[dict[str, str]]:
    """Retrieve existing entities for a file."""
    cursor.execute(
        """
        SELECT id, name, type FROM graph_nodes
        WHERE project = %s AND file_path = %s AND type <> 'file';
        """,
        (project, rel_path),
    )
    return [{"id": row[0], "name": row[1], "type": row[2]} for row in cursor.fetchall()]


def insert_edge(
    cursor: Cursor,
    project: str,
    source_id: str,
    target_id: str,
    relation_type: str,
    metadata: dict[str, object] | None = None,
) -> None:
    """Record one relation, ignoring a repeat of an edge already stored."""
    cursor.execute(
        """
        INSERT INTO graph_edges (
            project, source_id, target_id, relation_type, metadata
        )
        VALUES (%s, %s, %s, %s, %s::JSONB)
        ON CONFLICT DO NOTHING;
        """,
        (
            project,
            source_id,
            target_id,
            truncate(relation_type, MAX_TYPE_LENGTH),
            json.dumps(metadata or {"source": SOURCE_NATIVE}),
        ),
    )


def iter_nodes(
    cursor: Cursor, project: str
) -> list[tuple[str, str, str, str | None, str | None]]:
    """Read every node, for rebuilding the graph outside the database."""
    cursor.execute(
        """
        SELECT id, name, type, file_path, summary,
               COALESCE(metadata ->> 'community', '')
          FROM graph_nodes
         WHERE project = %s;
        """,
        (project,),
    )
    return cursor.fetchall()


def iter_edges(cursor: Cursor, project: str) -> list[tuple[str, str, str, str]]:
    """Read every edge, for rebuilding the graph outside the database."""
    cursor.execute(
        """
        SELECT source_id, target_id, relation_type,
               COALESCE(metadata ->> 'confidence', 'EXTRACTED')
          FROM graph_edges
         WHERE project = %s;
        """,
        (project,),
    )
    return cursor.fetchall()


def store_communities(
    cursor: Cursor, project: str, communities: dict[int, list[str]]
) -> int:
    """Write the community each node was clustered into back onto the node.

    Clustering runs over the merged graph, so this is what lets a node found
    by one producer share a community with nodes found by the other.
    """
    pairs = [
        (node_id, str(community_id))
        for community_id, members in communities.items()
        for node_id in members
    ]
    for node_id, community_id in pairs:
        cursor.execute(
            """
            UPDATE graph_nodes
               SET metadata = metadata || JSONB_BUILD_OBJECT('community', %s)
             WHERE project = %s AND id = %s;
            """,
            (community_id, project, node_id),
        )
    return len(pairs)


def vector_literal(vector: list[float]) -> str:
    """Render a vector the way pgvector parses it.

    Written by hand rather than through the pgvector adapter: this stack has
    exactly two statements that carry a vector, and a dependency the image
    would have to build for that is not worth the two casts.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def replace_file_embeddings(
    cursor: Cursor,
    project: str,
    node_id: str,
    content_hash: str,
    model: str,
    rows: list[tuple[int, int, int, str, list[float]]],
    chunk_chars: int = 0,
) -> int:
    """Write one file's chunks, replacing whatever it had. Returns the count.

    Replaced rather than merged: a file that lost half its lines would
    otherwise keep the chunks that used to hold them, and they would go on
    matching a query about code the file no longer contains.
    """
    cursor.execute(
        "DELETE FROM code_embeddings WHERE project = %s AND node_id = %s;",
        (project, node_id),
    )
    for index, start_line, end_line, text, vector in rows:
        cursor.execute(
            """
            INSERT INTO code_embeddings (
                project, node_id, chunk_index, start_line, end_line,
                content_chunk, content_hash, model, chunk_chars, embedding,
                updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector,
                CURRENT_TIMESTAMP
            );
            """,
            (
                project,
                node_id,
                index,
                start_line,
                end_line,
                text,
                content_hash,
                model,
                chunk_chars,
                vector_literal(vector),
            ),
        )
    return len(rows)


def embedding_coverage(cursor: Cursor, project: str) -> dict[str, int]:
    """How much of a project has vectors: chunks, files, and files indexed.

    The third number is what the first two are read against. A file with the
    embed skip bit is counted apart and left out of both: it is not owed.
    """
    cursor.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM code_embeddings WHERE project = %s),
            -- An empty file is finished with no chunk at all, so a done task
            -- counts as embedded as well as a row does.
            COUNT(*) FILTER (
                WHERE NOT s.skipped
                  AND (EXISTS (SELECT 1 FROM code_embeddings AS e
                                WHERE e.project = n.project AND e.node_id = n.id)
                       OR EXISTS (SELECT 1 FROM embed_tasks AS t
                                   WHERE t.project = n.project
                                     AND t.file_path = n.file_path
                                     AND t.status = 'done'))
            ),
            COUNT(*) FILTER (WHERE NOT s.skipped),
            COUNT(*) FILTER (WHERE s.skipped)
          FROM graph_nodes AS n
         CROSS JOIN LATERAL (
             SELECT (COALESCE((n.metadata ->> 'skip')::int, 0) & %s) <> 0
                    AS skipped
         ) AS s
         -- A file node without a path names no file - the tree root arrives
         -- as one - so it can never be embedded.
         WHERE n.project = %s AND n.type = 'file' AND n.file_path IS NOT NULL;
        """,
        (project, SKIP_EMBED, project),
    )
    chunks, files, indexed, skipped = cursor.fetchone()
    return {
        "chunks": int(chunks),
        "files": int(files),
        "indexed_files": int(indexed),
        "skipped": int(skipped),
    }


def summary_coverage(cursor: Cursor, project: str) -> dict[str, int]:
    """How much of a project the model has described, out of how much there is.

    The three counts are read against each other: `llm` is what a model wrote,
    `manual` is what a person wrote through the MCP tool and is never
    overwritten, and the rest carry the line the parser took from the head of
    the file. A file with the summarize skip bit is counted apart, not owed.
    """
    cursor.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE NOT s.skipped),
            COUNT(*) FILTER (
                WHERE NOT s.skipped AND n.metadata ->> 'summary_source' = 'llm'
            ),
            COUNT(*) FILTER (
                WHERE NOT s.skipped AND n.metadata ->> 'summary_source' = 'manual'
            ),
            COUNT(*) FILTER (WHERE s.skipped)
          FROM graph_nodes AS n
         CROSS JOIN LATERAL (
             SELECT (COALESCE((n.metadata ->> 'skip')::int, 0) & %s) <> 0
                    AS skipped
         ) AS s
         WHERE n.project = %s AND n.type = 'file' AND n.file_path IS NOT NULL;
        """,
        (SKIP_SUMMARIZE, project),
    )
    files, described, manual, skipped = cursor.fetchone()
    return {
        "files": int(files),
        "described": int(described),
        "manual": int(manual),
        "skipped": int(skipped),
    }
