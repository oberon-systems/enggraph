import { useState } from "react";

import { post, remove } from "../api.js";
import { Empty, ErrorBox, Icon, ICONS } from "./Common.js";
import { useApi } from "../hooks/useApi.js";
import type { Members as MemberList, Project } from "../types.js";

/** The projects an organization holds, and the ones it could take.
 *
 * Nothing moves: a member keeps its own tree, its own address and its own
 * graph, and belongs to as many organizations as list it. That is the whole
 * difference between this and the directories above it.
 */
export function Members({
  project,
  candidates,
}: {
  project: string;
  candidates: Project[];
}) {
  const path = `/projects/${encodeURIComponent(project)}/members`;
  const held = useApi<MemberList>(path);
  const [wanted, setWanted] = useState("");
  const [error, setError] = useState<string | null>(null);
  const members = held.data?.members ?? [];
  const names = new Set(members.map((one) => one.project));
  const free = candidates.filter(
    (one) => one.name !== project && !names.has(one.name),
  );

  async function run(work: () => Promise<unknown>) {
    setError(null);
    try {
      await work();
      held.reload();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <>
      <h2>Members</h2>
      {error !== null && <ErrorBox message={error} />}
      {members.length === 0 ? (
        <Empty>
          This organization holds no project yet. Add one below: it stays
          exactly where it is, and answers searches about this organization as
          well as its own.
        </Empty>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Project</th>
              <th>Reads</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.project}>
                <td>
                  <a href={`/projects/${encodeURIComponent(member.project)}`}>
                    {member.project}
                  </a>
                </td>
                <td className="path">
                  {member.sources.map((source) => source.root_path).join(", ")}
                </td>
                <td className="actions">
                  <button
                    type="button"
                    className="danger"
                    title={`Take ${member.project} out of ${project}; the project itself stays`}
                    aria-label={`Take ${member.project} out of ${project}`}
                    onClick={() =>
                      void run(() =>
                        remove(`${path}/${encodeURIComponent(member.project)}`),
                      )
                    }
                  >
                    <Icon path={ICONS.drop} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="filters">
        <label>
          Add a project, which stays where it is
          <select
            value={wanted}
            onChange={(event) => setWanted(event.target.value)}
          >
            <option value="">choose a project</option>
            {free.map((one) => (
              <option key={one.name} value={one.name}>
                {one.name}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          disabled={wanted === ""}
          onClick={() =>
            void run(async () => {
              await post(path, { project: wanted });
              setWanted("");
            })
          }
        >
          Add member
        </button>
      </div>

      <p className="muted">
        A member is indexed once, however many organizations hold it, and keeps
        its own <code>/mcp/&lt;name&gt;</code> address. While an organization
        lists a project, that project refuses to be dropped or moved into
        another one: take it out here first.
      </p>
    </>
  );
}
