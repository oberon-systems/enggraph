"""Which skills a session should have, and the words that tell it so."""

from __future__ import annotations

import re
from typing import Any

from enggraph.core import db

# The skill every session gets, whatever the switches say; the statement
# below names it too.
CORE_SKILL = "enggraph"

# Name clash: project > organization > global. Switch: the project's row, else
# any organization's, else on for a built-in or a skill of that very scope.
EFFECTIVE_SKILLS = """
WITH orgs AS (
  SELECT organization AS name FROM org_members WHERE project = $1
),
candidates AS (
  SELECT s.name, s.content, s.sha256, s.source, s.project AS owner,
         CASE WHEN s.project = $1 THEN 0
              WHEN s.project IS NOT NULL THEN 1
              ELSE 2 END AS rank,
         CASE WHEN s.name = 'enggraph' AND s.source = 'repo' THEN TRUE
              ELSE COALESCE(
                (SELECT e.enabled FROM skill_switches AS e
                  WHERE e.project = $1 AND e.skill_id = s.id),
                (SELECT bool_or(e.enabled) FROM skill_switches AS e
                   JOIN orgs AS o ON o.name = e.project
                  WHERE e.skill_id = s.id),
                s.source = 'repo' OR s.project IS NOT NULL)
         END AS enabled
    FROM agent_skills AS s
   WHERE s.project IS NULL OR s.project = $1
      OR s.project IN (SELECT name FROM orgs)
)
SELECT DISTINCT ON (name) name, content, sha256, source, owner
  FROM candidates
 WHERE enabled
 ORDER BY name, rank"""

FRONTMATTER = re.compile(r"\A---\r?\n([\s\S]*?)\r?\n---(\r?\n|\Z)")
VERSION_FIELD = re.compile(r"^version\s*:")


def effective_skills(project: str | None) -> list[db.Row]:
    """Return the skills a session on `project` should have installed."""
    return db.query(EFFECTIVE_SKILLS, [project or ""])


def skill_path(name: str) -> str:
    """Return where an agent keeps a skill, relative to the codebase root."""
    return f".claude/skills/{name}/SKILL.md"


def stamp(content: str, version: str) -> str:
    """Set `version:` in the frontmatter, adding the frontmatter when absent."""
    line = f"version: {version}"
    match = FRONTMATTER.match(content)
    if match is None:
        return f"---\n{line}\n---\n\n{content}"
    fields = [
        field
        for field in re.split(r"\r?\n", match.group(1))
        if not VERSION_FIELD.search(field)
    ]
    fields.append(line)
    rest = content[len(match.group(0)) :]
    return "---\n" + "\n".join(fields) + f"\n---{match.group(2)}{rest}"


def skill_instructions(skills: list[db.Row] | None) -> str:
    """Return the server instructions: what to install and how to update."""
    rule = (
        "Skills: each one this session should have is listed below with its "
        "version, the sha256 of its text. Before other work, compare every entry "
        "with the `version:` in the frontmatter of the local copy at "
        "`.claude/skills/<name>/SKILL.md`. When the copy is missing or its "
        "version differs, call `get_skill` and write the returned text to that "
        "path unchanged, then tell the user once which skills were installed or "
        "updated."
    )
    if skills is None:
        return f"{rule} The list could not be read; call `list_skills` for it."
    listing = [f"- {skill['name']} {skill['sha256']}" for skill in skills]
    return "\n".join([rule, "", *listing])


def with_skill_check(result: dict[str, Any], check: str) -> dict[str, Any]:
    """Append the skill check to a tool answer, after what the tool returned."""
    return {**result, "content": [*result["content"], {"type": "text", "text": check}]}
