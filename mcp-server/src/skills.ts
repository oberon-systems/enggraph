import type pg from "pg";

/** The skill every session gets, whatever the switches say. */
export const CORE_SKILL = "enggraph";

export interface SkillRow {
  name: string;
  content: string;
  sha256: string;
  source: string;
  /** The project or organization owning it, null for a global skill. */
  owner: string | null;
}

// Name clash: project > organization > global. Switch: the project's row, else
// any organization's, else on for a built-in or a skill of that very scope.
const EFFECTIVE_SKILLS = `
WITH orgs AS (
  SELECT organization AS name FROM project_members WHERE project = $1
),
candidates AS (
  SELECT s.name, s.content, s.sha256, s.source, s.project AS owner,
         CASE WHEN s.project = $1 THEN 0
              WHEN s.project IS NOT NULL THEN 1
              ELSE 2 END AS rank,
         CASE WHEN s.name = '${CORE_SKILL}' AND s.source = 'repo' THEN TRUE
              ELSE COALESCE(
                (SELECT e.enabled FROM skill_enablement AS e
                  WHERE e.project = $1 AND e.skill_id = s.id),
                (SELECT bool_or(e.enabled) FROM skill_enablement AS e
                   JOIN orgs AS o ON o.name = e.project
                  WHERE e.skill_id = s.id),
                s.source = 'repo' OR s.project IS NOT NULL)
         END AS enabled
    FROM skills AS s
   WHERE s.project IS NULL OR s.project = $1
      OR s.project IN (SELECT name FROM orgs)
)
SELECT DISTINCT ON (name) name, content, sha256, source, owner
  FROM candidates
 WHERE enabled
 ORDER BY name, rank`;

/** The skills a session on `project` should have installed. */
export async function effectiveSkills(
  pool: pg.Pool,
  project: string | null,
): Promise<SkillRow[]> {
  const res = await pool.query<SkillRow>(EFFECTIVE_SKILLS, [project ?? ""]);
  return res.rows;
}

/** Where an agent keeps a skill, relative to the codebase root. */
export function skillPath(name: string): string {
  return `.claude/skills/${name}/SKILL.md`;
}

/** Set `version:` in the frontmatter, adding the frontmatter when absent. */
export function stamp(content: string, version: string): string {
  const line = `version: ${version}`;
  const match = /^---\r?\n([\s\S]*?)\r?\n---(\r?\n|$)/.exec(content);
  if (match === null) {
    return `---\n${line}\n---\n\n${content}`;
  }
  const fields = match[1]
    .split(/\r?\n/)
    .filter((field) => !/^version\s*:/.test(field));
  fields.push(line);
  return `---\n${fields.join("\n")}\n---${match[2]}${content.slice(match[0].length)}`;
}

/** The server instructions: what to install and the rule for updating. */
export function skillInstructions(skills: SkillRow[] | null): string {
  const rule =
    "Skills: each one this session should have is listed below with its " +
    "version, the sha256 of its text. Before other work, compare every entry " +
    "with the `version:` in the frontmatter of the local copy at " +
    "`.claude/skills/<name>/SKILL.md`. When the copy is missing or its " +
    "version differs, call `get_skill` and write the returned text to that " +
    "path unchanged, then tell the user once which skills were installed or " +
    "updated.";
  if (skills === null) {
    return `${rule} The list could not be read; call \`list_skills\` for it.`;
  }
  const listing = skills.map((skill) => `- ${skill.name} ${skill.sha256}`);
  return [rule, "", ...listing].join("\n");
}
