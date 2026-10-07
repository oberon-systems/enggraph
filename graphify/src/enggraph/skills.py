"""Import the built-in skills from `skills/` into the database.

The MCP server hands them out with their sha256 as the version.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass

from psycopg2.extensions import cursor as Cursor

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skill:
    """One skill as read from disk."""

    name: str
    content: str
    sha256: str


def digest(content: str) -> str:
    """Return the version of a skill: sha256 of its text as stored."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_repo_skills(root: str) -> list[Skill]:
    """Every `<root>/<name>/SKILL.md`, sorted by name."""
    try:
        names = sorted(os.listdir(root))
    except OSError as error:
        LOG.warning("Skills directory %s is unreadable: %s", root, error)
        return []
    found = []
    for name in names:
        path = os.path.join(root, name, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except (OSError, UnicodeDecodeError) as error:
            LOG.warning("Skill %s is unreadable: %s", path, error)
            continue
        found.append(Skill(name, content, digest(content)))
    return found


def sync_repo_skills(cursor: Cursor, skills: list[Skill]) -> None:
    """Make the global `repo` rows match `skills`, leaving imports alone."""
    for skill in skills:
        cursor.execute(
            """
            INSERT INTO agent_skills (project, name, content, sha256, source)
            VALUES (NULL, %s, %s, %s, 'repo')
            ON CONFLICT (COALESCE(project, ''), name) DO UPDATE
            SET content = EXCLUDED.content,
                sha256 = EXCLUDED.sha256,
                source = 'repo',
                updated_at = CURRENT_TIMESTAMP
            WHERE agent_skills.sha256 <> EXCLUDED.sha256
               OR agent_skills.source <> 'repo';
            """,
            (skill.name, skill.content, skill.sha256),
        )
    cursor.execute(
        """
        WITH gone AS (
            DELETE FROM agent_skills
            WHERE source = 'repo' AND project IS NULL AND NOT (name = ANY(%s))
            RETURNING id
        )
        DELETE FROM skill_switches WHERE skill_id IN (SELECT id FROM gone);
        """,
        ([skill.name for skill in skills],),
    )


def sync_in_background(root: str, run: Callable[[list[Skill]], None]) -> None:
    """Import the skills baked into the image, off the startup path."""

    def sync() -> None:
        skills = read_repo_skills(root)
        if not skills:
            LOG.warning("No skills under %s; the table is left as it is", root)
            return
        try:
            run(skills)
        except Exception:  # noqa: BLE001 - startup must not die of it
            LOG.exception("Importing skills from %s failed", root)

    threading.Thread(target=sync, name="skill-sync", daemon=True).start()


def main() -> None:
    """Sync the built-in skills once and exit, for a stack without worker-api."""
    from enggraph.config import SKILLS_DIR
    from enggraph.storage import get_db_connection

    logging.basicConfig(level=logging.INFO)
    skills = read_repo_skills(SKILLS_DIR)
    if not skills:
        raise SystemExit(f"No skills under {SKILLS_DIR}")
    connection = get_db_connection()
    try:
        with connection, connection.cursor() as cursor:
            sync_repo_skills(cursor, skills)
    finally:
        connection.close()
    LOG.info("Synced %d built-in skills", len(skills))


if __name__ == "__main__":
    main()
