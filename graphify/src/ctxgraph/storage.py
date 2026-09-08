"""Every statement the indexer sends to PostgreSQL.

One database holds several projects, so every statement here is scoped to one.
The scope is passed in rather than read from the configuration: the caller
already knows which tree it is walking, and a module level default is exactly
what would let one project's re-index quietly delete another's rows.
"""

from __future__ import annotations

import json
import os
import posixpath

import psycopg2
from psycopg2.extensions import connection as Connection
from psycopg2.extensions import cursor as Cursor

from ctxgraph.config import (
    BUILTIN_PROJECT_TYPES,
    DEFAULT_PROJECT_TYPE,
    MAX_NAME_LENGTH,
    MAX_NODE_ID_LENGTH,
    MAX_TYPE_LENGTH,
    ORGANIZATION_PROJECT_TYPE,
    SOURCE_NATIVE,
)
from ctxgraph.identifiers import (
    entity_node_id,
    project_name,
    source_alias,
    truncate,
)

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


def list_sources(cursor: Cursor, project: str) -> list[tuple[str, str]]:
    """Read the directories one project is built from, as (alias, host path).

    The empty alias is a project mounted whole at `/code/<project>`, which is
    what every project was before a project could hold more than one directory.
    Ordered the way the mounts are written, so the first row is the primary.
    """
    cursor.execute(
        """
        SELECT alias, root_path FROM project_sources
         WHERE project = %s ORDER BY created_at, alias;
        """,
        (project,),
    )
    return [(str(alias), str(root_path)) for alias, root_path in cursor.fetchall()]


def source_owner(cursor: Cursor, root_path: str) -> str | None:
    """Return the project a host directory already belongs to, if any."""
    cursor.execute(
        "SELECT project FROM project_sources WHERE root_path = %s;", (root_path,)
    )
    row = cursor.fetchone()
    return str(row[0]) if row is not None else None


def set_primary(cursor: Cursor, project: str) -> None:
    """Point projects.root_path at the tree the project is, if it is one.

    A project mounted whole is a tree, and the column names it: that is what
    the worker API, the backup script and the dashboard address it by.

    A project of named directories is not a tree. It is a container - a
    monorepo in slices, or a thematic one collecting projects so a search
    reaches all of them at once - and naming whichever slice arrived first
    would let one directory stand in for the project. It keeps the synthetic
    root instead, exactly as a project registered before it read anything
    does, which is also what a project whose last directory left goes back to.
    """
    sources = dict(list_sources(cursor, project))
    root = sources.get("", registered_root(project))
    cursor.execute(
        """
        UPDATE projects SET root_path = %s
         WHERE name = %s AND root_path <> %s;
        """,
        (root, project, root),
    )


def add_source(cursor: Cursor, project: str, alias: str, root_path: str) -> None:
    """Record one more directory a project is built from.

    A project holds either a single unnamed source or several named ones.
    Mixing the two would nest one bind mount inside another and index the same
    files twice, under two ids, so it is refused rather than resolved.
    """
    owner = source_owner(cursor, root_path)
    if owner is not None and owner != project:
        raise RuntimeError(
            f"{root_path!r} is already a source of project {owner!r}; "
            "one directory belongs to one project"
        )
    cursor.execute(
        "SELECT name FROM projects WHERE root_path = %s AND name <> %s;",
        (root_path, project),
    )
    row = cursor.fetchone()
    if row is not None:
        raise RuntimeError(
            f"{root_path!r} is where project {row[0]!r} was onboarded; "
            "onboard this one from a directory of its own"
        )
    sources = dict(list_sources(cursor, project))
    if alias in sources:
        if sources[alias] != root_path:
            raise RuntimeError(
                f"project {project!r} already reads {alias!r} from "
                f"{sources[alias]!r}; drop that source before pointing the "
                "alias somewhere else"
            )
        return
    if alias and "" in sources:
        raise RuntimeError(
            f"project {project!r} is mounted whole from {sources['']!r}; name "
            f"its root first, with `make source-promote PROJECT_NAME={project} "
            "ALIAS=<alias>`, then add this one"
        )
    if not alias and sources:
        raise RuntimeError(
            f"project {project!r} already reads named directories "
            f"({', '.join(sorted(sources))}); pass an alias for {root_path!r}"
        )
    cursor.execute(
        """
        INSERT INTO project_sources (project, alias, root_path)
        VALUES (%s, %s, %s);
        """,
        (project, alias, root_path),
    )
    set_primary(cursor, project)


def ensure_source(cursor: Cursor, project: str, alias: str, root_path: str) -> None:
    """Record a directory as a source unless the project already reads it.

    Registering and indexing both arrive with a host path that is usually
    already stored, so the alias only decides what a directory the project has
    never seen is called.
    """
    cursor.execute(
        "SELECT alias FROM project_sources WHERE project = %s AND root_path = %s;",
        (project, root_path),
    )
    if cursor.fetchone() is not None:
        return
    add_source(cursor, project, alias, root_path)


def drop_source(cursor: Cursor, project: str, alias: str) -> None:
    """Stop reading one directory of a project.

    The nodes it produced stay until the next index run prunes them, which is
    the same path a deleted file takes. Its settings do not: a row keyed on an
    alias the project no longer reads describes nothing, is listed nowhere,
    and would silently decide the selection again if that alias ever came
    back.
    """
    sources = dict(list_sources(cursor, project))
    if alias not in sources:
        raise RuntimeError(
            f"project {project!r} has no source {alias!r}; it reads "
            f"{', '.join(repr(name) for name in sorted(sources)) or 'nothing'}"
        )
    if len(sources) == 1:
        raise RuntimeError(
            f"{alias!r} is the only source of project {project!r}; drop the "
            "project itself rather than leaving it with no tree"
        )
    cursor.execute(
        "DELETE FROM project_sources WHERE project = %s AND alias = %s;",
        (project, alias),
    )
    clear_settings(cursor, project, alias)
    set_primary(cursor, project)


def promote_root(cursor: Cursor, project: str, alias: str) -> None:
    """Give the unnamed source of a project a name, so a second one can join.

    Every node id gains the alias as its first segment. Nothing rewrites them
    here: the next index run discovers the files under their new paths, and
    `prune_missing_files` deletes the nodes and hashes left at the old ones.
    """
    sources = dict(list_sources(cursor, project))
    if "" not in sources:
        raise RuntimeError(
            f"project {project!r} has no unnamed source to promote; it reads "
            f"{', '.join(repr(name) for name in sorted(sources)) or 'nothing'}"
        )
    if alias in sources:
        raise RuntimeError(f"project {project!r} already reads {alias!r}")
    cursor.execute(
        "UPDATE project_sources SET alias = %s WHERE project = %s AND alias = '';",
        (alias, project),
    )


def stored_type(cursor: Cursor, project: str) -> str | None:
    """Return what kind of project this is, or None when there is no row.

    Named for the column rather than for the argument every other function
    here calls `project_type`, which is the type a caller is asking to store.
    """
    cursor.execute("SELECT type FROM projects WHERE name = %s;", (project,))
    row = cursor.fetchone()
    return str(row[0]) if row is not None else None


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


def add_member(cursor: Cursor, organization: str, project: str) -> None:
    """Add one project to an organization, leaving the project untouched.

    Nothing moves and nothing is copied: the member keeps its name, its
    address and its graph, and belongs to as many organizations as list it.
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
    cursor.execute(
        """
        INSERT INTO project_members (organization, project)
        VALUES (%s, %s) ON CONFLICT DO NOTHING;
        """,
        (organization, project),
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


def drop_emptied(cursor: Cursor, donor: str, target: str) -> None:
    """Retire a project whose directories all went to another one.

    Its records follow the directories rather than being orphaned: the name
    they describe is about to stop existing, which is the whole difference
    between a project moving in and a directory moving.
    """
    cursor.execute(
        """
        UPDATE graph_nodes
           SET metadata = jsonb_set(metadata, '{about}', to_jsonb(%s::text))
         WHERE project = %s AND metadata ->> 'about' = %s;
        """,
        (target, PLANS_PROJECT, donor),
    )
    cursor.execute(
        """
        UPDATE graph_nodes
           SET id = %s || substring(id from position('/' in id) + 1),
               metadata = jsonb_set(metadata, '{about}', to_jsonb(%s::text))
         WHERE project = ANY(%s) AND metadata ->> 'about' = %s;
        """,
        (f"{target}/", target, list(SCOPED_RECORD_PROJECTS), donor),
    )
    # No foreign key reaches this table, so the cascade below would leave the
    # donor's runs behind under a name that will never exist again.
    cursor.execute("DELETE FROM index_jobs WHERE project = %s;", (donor,))
    cursor.execute("DELETE FROM projects WHERE name = %s;", (donor,))


def relocate_source(
    cursor: Cursor, project: str, alias: str, target: str, new_alias: str
) -> None:
    """Re-key one directory and its selection onto another project.

    The two statements every move shares and none of the rules: each caller has
    already settled that the move is allowed.
    """
    cursor.execute(
        """
        UPDATE project_settings SET project = %s, alias = %s
         WHERE project = %s AND alias = %s;
        """,
        (target, new_alias, project, alias),
    )
    cursor.execute(
        """
        UPDATE project_sources SET project = %s, alias = %s
         WHERE project = %s AND alias = %s;
        """,
        (target, new_alias, project, alias),
    )


def discard_source_graph(cursor: Cursor, project: str, alias: str) -> None:
    """Delete what a project derived from a directory that has left it.

    `drop_source` leaves this to the next index run, because a dropped
    directory may be added back. A moved one belongs to another project, and a
    project whose last directory left is never indexed again, so waiting for a
    prune here would strand the rows for good.
    """
    if alias:
        cursor.execute(
            "DELETE FROM graph_nodes WHERE project = %s AND starts_with(id, %s);",
            (project, f"{alias}/"),
        )
        cursor.execute(
            """
            DELETE FROM file_hashes
             WHERE project = %s AND starts_with(file_path, %s);
            """,
            (project, f"{alias}/"),
        )
        return
    cursor.execute("DELETE FROM graph_nodes WHERE project = %s;", (project,))
    cursor.execute("DELETE FROM file_hashes WHERE project = %s;", (project,))


def absorb_project(
    cursor: Cursor, target: str, donor: str, alias: str
) -> dict[str, object]:
    """Move every directory of one project into another and drop the donor.

    A tree onboarded on its own turns out to be part of a bigger one. Its
    directories move under an alias each, the records written about it follow
    its name, and the row goes with everything the cascade owns.

    Node ids are not rewritten here, exactly as `promote_root` does not rewrite
    them: the next index run of the target discovers the files under their
    prefixed paths and `prune_missing_files` retires what was left behind.
    """
    if target == donor:
        raise RuntimeError(f"project {target!r} cannot absorb itself")
    for name in (target, donor):
        cursor.execute("SELECT type FROM projects WHERE name = %s;", (name,))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no project named {name!r}")
        if row[0] in BUILTIN_PROJECT_TYPES:
            raise RuntimeError(
                f"project {name!r} holds agent {row[0]}, not an indexed tree; "
                "there is no directory to move"
            )

    donor_sources = list_sources(cursor, donor)
    if not donor_sources:
        raise RuntimeError(
            f"project {donor!r} reads no directory; drop it rather than moving "
            f"nothing into {target!r}"
        )
    held = dict(list_sources(cursor, target))
    if "" in held:
        raise RuntimeError(
            f"project {target!r} is mounted whole from {held['']!r}; name its "
            f"root first, with `make source-promote PROJECT_NAME={target} "
            "ALIAS=<alias>`, then move a project into it"
        )
    cursor.execute(
        """
        SELECT project FROM index_jobs
         WHERE project IN (%s, %s) AND status = 'running';
        """,
        (target, donor),
    )
    running = cursor.fetchone()
    if running is not None:
        raise RuntimeError(
            f"project {running[0]!r} is being indexed; wait for that run to "
            "finish, because the move changes what it walks"
        )

    named = [name for name, _ in donor_sources if name]
    if named and alias.strip():
        raise RuntimeError(
            f"project {donor!r} already reads named directories "
            f"({', '.join(sorted(named))}); each keeps the alias it has, so "
            "the move takes none"
        )
    moved: list[tuple[str, str, str]] = []
    for old, root_path in donor_sources:
        new = old or source_alias(alias.strip() or donor, root_path)
        if new in held:
            raise RuntimeError(
                f"project {target!r} already reads {new!r} from {held[new]!r}; "
                "drop that directory or move this one under another alias"
            )
        held[new] = root_path
        moved.append((old, new, root_path))

    require_unheld(cursor, donor, f"moving it into {target!r}")
    records = read_records_about(cursor, donor)
    check_record_scope(cursor, records, donor, target)

    for old, new, _ in moved:
        relocate_source(cursor, donor, old, target, new)
    drop_emptied(cursor, donor, target)
    set_primary(cursor, target)
    return {
        "sources": [
            {"alias": new, "root_path": root_path, "was": old}
            for old, new, root_path in moved
        ],
        **count_records(records),
    }


def move_source(
    cursor: Cursor,
    project: str,
    alias: str,
    target: str,
    new_alias: str,
    drop_empty: bool = False,
) -> dict[str, object]:
    """Move one directory of a project into another project.

    What the target reads decides the name: a project already reading named
    directories takes another named one, and a project reading nothing may take
    this one whole, which is what `detach_source` is built on.

    Node ids carry the alias, so the target has to be indexed again, and what
    the donor derived from the directory is discarded rather than left behind.

    `drop_empty` settles what a project left with nothing is: moving a
    project's only directory into another one is that project moving, so the
    row goes and the records written about its name follow the directory.
    Detaching does not pass it - a container that hands a slice back is meant
    to outlive it and take another.
    """
    if project == target:
        raise RuntimeError(f"project {project!r} already reads {alias!r}")
    sources = dict(list_sources(cursor, project))
    if alias not in sources:
        raise RuntimeError(
            f"project {project!r} has no source {alias!r}; it reads "
            f"{', '.join(repr(name) for name in sorted(sources)) or 'nothing'}"
        )
    root_path = sources[alias]
    cursor.execute("SELECT type FROM projects WHERE name = %s;", (target,))
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"no project named {target!r}")
    if row[0] in BUILTIN_PROJECT_TYPES:
        raise RuntimeError(
            f"project {target!r} holds agent {row[0]}, not an indexed tree; "
            "no directory is read into it"
        )
    held = dict(list_sources(cursor, target))
    if "" in held:
        raise RuntimeError(
            f"project {target!r} is mounted whole from {held['']!r}; name its "
            f"root first, with `make source-promote PROJECT_NAME={target} "
            "ALIAS=<alias>`, then move a directory into it"
        )
    cursor.execute(
        """
        SELECT project FROM index_jobs
         WHERE project IN (%s, %s) AND status = 'running';
        """,
        (project, target),
    )
    running = cursor.fetchone()
    if running is not None:
        raise RuntimeError(
            f"project {running[0]!r} is being indexed; wait for that run to "
            "finish, because the move changes what it walks"
        )
    wanted = new_alias.strip()
    if held:
        # The name a directory is known by, in order: what the caller asked
        # for, what it is called here, then the project it is leaving - which
        # is the only one of the three a whole project moving in ever has.
        settled = source_alias(wanted or alias or project, root_path)
    else:
        settled = source_alias(wanted, root_path) if wanted else ""
    if settled in held:
        raise RuntimeError(
            f"project {target!r} already reads {settled!r} from "
            f"{held[settled]!r}; drop that directory, or move this one under "
            "another alias"
        )
    emptied = drop_empty and len(sources) == 1
    records = read_records_about(cursor, project) if emptied else []
    if emptied:
        require_unheld(cursor, project, f"moving it into {target!r}")
        check_record_scope(cursor, records, project, target)
    relocate_source(cursor, project, alias, target, settled)
    discard_source_graph(cursor, project, alias)
    if emptied:
        drop_emptied(cursor, project, target)
    else:
        set_primary(cursor, project)
    set_primary(cursor, target)
    return {
        "project": target,
        "alias": settled,
        "root_path": root_path,
        "was": alias,
        "left": project,
        "dropped": emptied,
        **count_records(records),
    }


def detach_source(
    cursor: Cursor,
    project: str,
    alias: str,
    new_project: str,
    project_type: str | None = None,
) -> dict[str, object]:
    """Take one directory out of a project and make a project of it.

    The inverse of absorbing. The tree ends up mounted whole, with the
    unprefixed node ids an ordinary onboarded tree carries, so it means nothing
    until it is indexed - and neither does the row it leaves behind, which is a
    project reading nothing until another directory arrives.
    """
    sources = dict(list_sources(cursor, project))
    if alias not in sources:
        raise RuntimeError(
            f"project {project!r} has no source {alias!r}; it reads "
            f"{', '.join(repr(name) for name in sorted(sources)) or 'nothing'}"
        )
    name = project_name(new_project, sources[alias])
    cursor.execute("SELECT 1 FROM projects WHERE name = %s;", (name,))
    if cursor.fetchone() is not None:
        raise RuntimeError(
            f"project {name!r} already exists; move the directory into it "
            "rather than detaching it, or detach it under another name"
        )
    register_project(
        cursor,
        name,
        registered_root(name),
        project_type,
        with_source=False,
    )
    return move_source(cursor, project, alias, name, "")


def read_settings(
    cursor: Cursor, project: str, alias: str
) -> tuple[str | None, str | None]:
    """Read one settings row as (ctxkeep, ctxignore), without any fallback.

    The precedence between the levels is `ctxgraph.selection`'s business, so
    this answers about the one row it was asked for and nothing else. A missing
    row and a row holding two NULLs are the same answer on purpose: both mean
    this level says nothing about the selection.
    """
    cursor.execute(
        "SELECT ctxkeep, ctxignore FROM project_settings "
        "WHERE project = %s AND alias = %s;",
        (project, alias),
    )
    row = cursor.fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def write_settings(
    cursor: Cursor,
    project: str,
    alias: str,
    ctxkeep: str | None,
    ctxignore: str | None,
) -> None:
    """Store the selection documents for one level, verbatim.

    Both are written every time, NULL included: clearing one document is how a
    level stops speaking for it and lets the level above answer instead.
    """
    cursor.execute(
        """
        INSERT INTO project_settings (project, alias, ctxkeep, ctxignore)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (project, alias) DO UPDATE SET
            ctxkeep = EXCLUDED.ctxkeep,
            ctxignore = EXCLUDED.ctxignore,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (project, alias, ctxkeep, ctxignore),
    )


def clear_settings(cursor: Cursor, project: str, alias: str) -> None:
    """Drop one settings row, so the level above it takes over again."""
    cursor.execute(
        "DELETE FROM project_settings WHERE project = %s AND alias = %s;",
        (project, alias),
    )


def read_settings_json(cursor: Cursor, project: str, alias: str) -> dict:
    """Read the settings object of one level, without any fallback.

    The two selection documents are columns of their own; everything else a
    level says - the indexing schedule today - is one JSONB object, so a new
    knob is a key rather than a migration.
    """
    cursor.execute(
        "SELECT settings FROM project_settings WHERE project = %s AND alias = %s;",
        (project, alias),
    )
    row = cursor.fetchone()
    if row is None or not isinstance(row[0], dict):
        return {}
    return row[0]


def write_settings_json(
    cursor: Cursor, project: str, alias: str, key: str, value: dict | None
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
             WHERE project = %s AND alias = %s;
            """,
            (key, project, alias),
        )
        return
    cursor.execute(
        """
        INSERT INTO project_settings (project, alias, settings)
        VALUES (%s, %s, %s::jsonb)
        ON CONFLICT (project, alias) DO UPDATE SET
            settings = project_settings.settings || EXCLUDED.settings,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (project, alias, json.dumps({key: value})),
    )


def has_settings(cursor: Cursor, project: str) -> bool:
    """Report whether a project holds a settings row at any of its levels.

    Onboarding writes the generated pair only into a project that has none,
    which is the same rule that kept it from replacing a file already in a
    tree.
    """
    cursor.execute(
        "SELECT 1 FROM project_settings WHERE project = %s LIMIT 1;",
        (project,),
    )
    return cursor.fetchone() is not None


def set_selection_origin(
    cursor: Cursor, project: str, alias: str, keep: str, ignore: str
) -> None:
    """Record where an index run read one source's selection from.

    The dashboard holds no mount and cannot look at a tree, so the run that
    did the looking is what reports it.
    """
    cursor.execute(
        """
        UPDATE project_sources
           SET keep_source = %s, ignore_source = %s
         WHERE project = %s AND alias = %s;
        """,
        (keep, ignore, project, alias),
    )


def check_project_identity(cursor: Cursor, project: str, root_path: str) -> None:
    """Refuse a name/path pairing that would merge or orphan a graph.

    Both directions are checked, and neither is repaired silently. A name
    pointing at a new path means two checkouts share a directory name, and
    letting the second one through would merge two unrelated codebases into
    one graph. A path arriving under a new name means a rename, which is
    legitimate but has to move the existing rows rather than orphan them, so
    it is refused here rather than half done. A project reading named
    directories is exempt from the first half: taking another path is what it
    is for, and `add_source` is where that is decided.
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

    sources = dict(list_sources(cursor, project))
    # A project mounted whole reads one directory and only that one. A project
    # reading named directories is expected to gain more, which is what
    # `add_source` is for, so an unknown path is only a collision here.
    if "" in sources and sources[""] != root_path:
        raise RuntimeError(
            f"project {project!r} is already indexed from {sources['']!r}; "
            f"pass PROJECT_NAME to index {root_path!r} under another name"
        )

    owner = source_owner(cursor, root_path)
    if owner is not None and owner != project:
        raise RuntimeError(
            f"{root_path!r} is already indexed as {owner!r}; "
            f"rename it in the projects table before indexing it as {project!r}"
        )


def ensure_project(
    cursor: Cursor,
    project: str,
    root_path: str,
    project_type: str | None = None,
    alias: str = "",
) -> None:
    """Register the project being indexed, or refresh when it was.

    `project_type` categorises the project for the cross-project MCP search.
    None means "leave whatever is stored alone", so a plain re-index does not
    demote a project that was registered as something other than the default.
    """
    check_project_identity(cursor, project, root_path)
    cursor.execute("SELECT 1 FROM projects WHERE name = %s;", (project,))
    # A registered project holding no directory is one onboarded ahead of its
    # slices. Registering `root_path` as its tree here is exactly what its
    # owner avoided by onboarding it empty, so the run stops instead.
    if cursor.fetchone() is not None and not list_sources(cursor, project):
        raise RuntimeError(
            f"project {project!r} reads no directories yet; add one with "
            f"`make source-add PROJECT=<host path> PROJECT_NAME={project} "
            "ALIAS=<alias>` before indexing it"
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
    # A project of named directories keeps the synthetic root `set_primary`
    # gives it, and that root is not a directory anything reads.
    if root_path != registered_root(project):
        ensure_source(cursor, project, alias, root_path)


def register_project(
    cursor: Cursor,
    project: str,
    root_path: str,
    project_type: str | None = None,
    alias: str = "",
    with_source: bool = True,
) -> None:
    """Record a tree as a project without claiming it has been indexed.

    Onboarding writes the row so the tree is listed, mounted and offered an
    index run; the graph itself comes later. `indexed_at` is therefore left
    alone in both branches - NULL on the insert, untouched on the update - so
    a project already indexed keeps its freshness when it is onboarded again.

    `with_source` false writes the row and no directory at all, which is how a
    monorepo is onboarded before its slices are added one at a time. Until the
    first source arrives, `root_path` is where the project was onboarded from
    rather than a directory anything reads.
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
    if with_source:
        ensure_source(cursor, project, alias, root_path)


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


def list_all_sources(cursor: Cursor) -> list[tuple[str, str, str]]:
    """Read every mountable directory as (project, alias, host path).

    The built-in projects hold records rather than files and never gain a
    source; they are excluded here as well, so the listing cannot grow one by
    accident.
    """
    cursor.execute(
        """
        SELECT s.project, s.alias, s.root_path
          FROM project_sources AS s
          JOIN projects AS p ON p.name = s.project
         WHERE NOT (p.type = ANY(%s))
         ORDER BY s.project, s.created_at, s.alias;
        """,
        (list(BUILTIN_PROJECT_TYPES),),
    )
    return [
        (str(project), str(alias), str(root_path))
        for project, alias, root_path in cursor.fetchall()
    ]


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


def prune_missing_files(
    cursor: Cursor, project: str, known_paths: list[str], alias: str = ""
) -> int:
    """Delete everything derived from a file that is no longer in the tree.

    A re-index only visits the files it finds, so a file that was renamed or
    deleted is never reached by the per-file cleanup and its nodes outlive it.
    The set of files just discovered is the only thing that knows they are
    gone.

    `alias` is what makes one directory indexable on its own: a run that walked
    a single source has discovered nothing about the others, and without this
    it would read their absence as deletion and take their whole graph with it.
    """
    if not known_paths:
        return 0
    scope = f"{alias}/" if alias else ""
    cursor.execute(
        "DELETE FROM graph_nodes WHERE project = %s AND file_path IS NOT NULL "
        "AND NOT (file_path = ANY(%s)) "
        "AND (%s = '' OR starts_with(file_path, %s));",
        (project, known_paths, scope, scope),
    )
    removed = cursor.rowcount
    cursor.execute(
        "DELETE FROM file_hashes WHERE project = %s AND NOT (file_path = ANY(%s)) "
        "AND (%s = '' OR starts_with(file_path, %s));",
        (project, known_paths, scope, scope),
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
