import { useState } from "react";

import { put } from "../api.js";
import { Empty, ErrorBox, Spinner } from "./Common.js";
import { useApi } from "../hooks/useApi.js";
import type { ProjectSkill } from "../types.js";

export function SkillSwitches({ project }: { project: string }) {
  const path = `/projects/${encodeURIComponent(project)}/skills`;
  const skills = useApi<ProjectSkill[]>(path);
  const [error, setError] = useState<string | null>(null);

  async function toggle(skill: ProjectSkill) {
    try {
      await put(`${path}/${skill.id}`, { enabled: !skill.enabled });
      setError(null);
      skills.reload();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  if (skills.error !== null) {
    return <ErrorBox message={skills.error} />;
  }
  if (skills.data === null) {
    return <Spinner what="skills" />;
  }
  if (skills.data.length === 0) {
    return <Empty>No skill reaches this project.</Empty>;
  }

  return (
    <>
      <p className="muted">
        The skills an agent on {project} is told to install. Unset switches
        follow an organization holding it, else built-in and own skills are on.{" "}
        <code>enggraph</code> is always on.
      </p>
      {error !== null && <ErrorBox message={error} />}
      <table className="grid">
        <thead>
          <tr>
            <th>On</th>
            <th>Skill</th>
            <th>Owner</th>
            <th>Version</th>
            <th>Set</th>
          </tr>
        </thead>
        <tbody>
          {skills.data.map((skill) => (
            <tr key={skill.id}>
              <td>
                <input
                  type="checkbox"
                  checked={skill.enabled}
                  disabled={skill.locked}
                  onChange={() => void toggle(skill)}
                />
              </td>
              <td>{skill.name}</td>
              <td>{skill.owner ?? "global"}</td>
              <td className="muted id">{skill.sha256.slice(0, 12)}</td>
              <td className="muted">
                {skill.locked
                  ? "always"
                  : skill.explicit === null
                    ? "default"
                    : "here"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
