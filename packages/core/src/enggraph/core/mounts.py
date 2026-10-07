"""List the directories the stack should mount, one line per project.

A project is one host tree, mounted at `CODE_ROOT/<project>`. Neither the
project name nor the mount point is a choice a caller gets to make: both come
from the same rules the indexer applies, so a mount and the row it belongs to
cannot disagree.

`--register` is what makes that exact rather than nearly true: onboarding
stores the directory here, so the list is the projects table and nothing else.
The row carries no `indexed_at`, because registering a tree is not indexing it
- the dashboard offers that afterwards. `--create` registers a project that
reads no directory at all, which is how an organization is onboarded before
the projects it holds are moved into it.

The output is a list rather than the compose file itself because only the host
can tell whether a directory still exists, and a bind mount whose source is
missing is created as an empty directory rather than refused - which would
index an empty tree and prune a whole graph. `scripts/mounts.sh` does that
check and writes the file.
"""

from __future__ import annotations

import argparse
import sys

from enggraph.core.config import KNOWN_PROJECT_TYPES
from enggraph.core.identifiers import project_name
from enggraph.core.storage import (
    get_db_connection,
    list_mountable_projects,
    register_project,
    registered_root,
)


def parse_args() -> argparse.Namespace:
    """Read the change to make, if any, before the listing is taken."""
    parser = argparse.ArgumentParser(
        prog="enggraph.core.mounts",
        description="List every directory to mount, as project and path.",
    )
    parser.add_argument(
        "--add",
        default="",
        help="host path of a directory being mounted for the first time",
    )
    parser.add_argument(
        "--name",
        default="",
        help="project for --add; derived from its last path segment when unset",
    )
    # Whether --add is stored or only listed. Onboarding stores it; every
    # other caller lists, because a bare `make mounts` reaches here with a
    # path of its own and is not onboarding anything. That the path exists
    # is checked by the host afterwards, so only a caller that has already
    # checked should pass this.
    parser.add_argument(
        "--register",
        action="store_true",
        help="store --add as a project before listing, without indexing it",
    )
    # A project onboarded ahead of its tree. The row exists so the dashboard
    # lists it and the agent files have an address to name, and nothing is
    # mounted or read for it.
    parser.add_argument(
        "--create",
        action="store_true",
        help="register --add as a project reading no directory yet",
    )
    parser.add_argument(
        "--type",
        default="",
        dest="project_type",
        help="type for --register; empty keeps the stored one",
    )
    return parser.parse_args()


def register(
    root_path: str,
    name: str,
    project_type: str,
    with_tree: bool = True,
) -> str:
    """Store one directory as a project and return the name it is known by."""
    project = project_name(name, root_path)
    if project_type and project_type not in KNOWN_PROJECT_TYPES:
        print(
            f"unknown project type {project_type!r}, expected one of "
            f"{', '.join(sorted(KNOWN_PROJECT_TYPES))}",
            file=sys.stderr,
        )
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            register_project(
                cursor,
                project,
                root_path if with_tree else registered_root(project),
                project_type or None,
            )
        conn.commit()
    finally:
        conn.close()
    if not with_tree:
        print(
            f"registered {project}, onboarded from {root_path} and reading no "
            "directory yet",
            file=sys.stderr,
        )
        return project
    print(f"registered {project} at {root_path}", file=sys.stderr)
    return project


def mounted_trees() -> dict[str, str]:
    """Return the host path of every project holding a real tree."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            return dict(list_mountable_projects(cursor))
    finally:
        conn.close()


def main() -> None:
    """Print one `project<TAB>host path` line per project that is a tree."""
    args = parse_args()
    root_path = args.add.rstrip("/")

    if root_path and args.register:
        register(
            root_path,
            args.name,
            args.project_type.strip(),
            not args.create,
        )

    trees = mounted_trees()
    if root_path and not args.create:
        # The same rule `resolve_project` applies, so indexing this tree lands
        # in the project its mount was written for. A registered directory is
        # already in the listing above; this is what carries an unregistered
        # one, which is how a first mount happens before the row exists.
        trees[project_name(args.name, root_path)] = root_path
    if not trees:
        print("nothing is indexed yet, so nothing is mounted", file=sys.stderr)
    for project in sorted(trees):
        print(f"{project}\t{trees[project]}")


if __name__ == "__main__":
    main()
