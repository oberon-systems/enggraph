"""One-shot embedding pass: `python -m enggraph.embed`.

The queue in the worker API is the way a project keeps its vectors current.
This is the other way: enqueue a project and drain it here, in the foreground,
until there is nothing left. It exists for the case the queue is bad at - a
first pass over a large tree that somebody wants to watch finish - and for a
stack where the API is not running at all.

It respects the switch. A project with embedding off has said it does not want
vectors, and a pass that ignored that would fill a table the dashboard reports
as empty.
"""

from __future__ import annotations

import argparse
import logging

from psycopg2.extensions import connection as Connection

from enggraph import embedjobs, features
from enggraph.config import (
    EMBED_LEASE_SECONDS,
    EMBED_MODEL,
    FEATURE_EMBEDDING,
    PROJECT_ROOT,
)
from enggraph.embedder import Embedder
from enggraph.embedloop import EmbedLoop
from enggraph.identifiers import project_name
from enggraph.storage import get_db_connection, list_mountable_projects

LOG = logging.getLogger(__name__)

# How many files one claim takes. Larger than the loop's per-tick budget: this
# pass is what somebody is waiting for, rather than background work.
BATCH = 16


def parse_args() -> argparse.Namespace:
    """Read which project to embed. Nothing named means every one."""
    parser = argparse.ArgumentParser(
        prog="enggraph.embed",
        description="Write vectors for the files that have none.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="every indexed project, which is also what an unset PROJECT_ROOT means",
    )
    parser.add_argument(
        "--project",
        default="",
        help="one project by name",
    )
    return parser.parse_args()


def targets(conn: Connection, named: str, everything: bool) -> list[str]:
    """Return the projects this run covers, each checked against its own switch."""
    with conn.cursor() as cursor:
        mounted = [project for project, _ in list_mountable_projects(cursor)]
        wanted = mounted if everything or not named else [named]
        allowed = []
        for project in wanted:
            if project not in mounted:
                LOG.warning("%s is not mounted; run `make mounts`", project)
                continue
            settled = features.resolve(cursor, project, FEATURE_EMBEDDING)
            if not settled.enabled:
                where = "globally" if settled.gated else "for this project"
                LOG.warning("Embedding is switched off %s; skipping %s", where, project)
                continue
            allowed.append(project)
    conn.commit()
    return allowed


def embed_project(conn: Connection, loop: EmbedLoop, project: str) -> int:
    """Enqueue a project and drain it. Returns the files embedded."""
    with conn.cursor() as cursor:
        settled = features.resolve(cursor, project, FEATURE_EMBEDDING)
        queued = embedjobs.enqueue_project(
            cursor, project, EMBED_MODEL, settled.chunk_chars
        )
    conn.commit()
    LOG.info("%s: %d file(s) to embed", project, queued)

    embedder = Embedder()
    done = 0
    while True:
        with conn.cursor() as cursor:
            tasks = embedjobs.claim(cursor, [project], BATCH, EMBED_LEASE_SECONDS)
        conn.commit()
        if not tasks:
            return done
        for task in tasks:
            loop.embed_file(conn, embedder, task)
            done += 1
        LOG.info("%s: %d file(s) done", project, done)


def main() -> None:
    """Embed one project, or every project that asked for it."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    named = args.project or (project_name(PROJECT_ROOT) if PROJECT_ROOT else "")
    conn = get_db_connection()
    try:
        loop = EmbedLoop()
        total = 0
        for project in targets(conn, named, args.auto or not named):
            total += embed_project(conn, loop, project)
        LOG.info("Embedded %d file(s)", total)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
