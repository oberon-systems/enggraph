import { useCallback, useState } from "react";
import { Link } from "react-router";

import { post, remove } from "../api.js";
import {
  Empty,
  ErrorBox,
  Icon,
  ICONS,
  IndexedAge,
  SCHEDULE_LEVELS,
  scheduleDetail,
} from "./Common.js";
import { ConfirmModal } from "./ConfirmModal.js";
import { IndexButton } from "./IndexButton.js";
import { useApi } from "../hooks/useApi.js";
import type {
  Members as MemberList,
  Project,
  ScheduleSummary,
} from "../types.js";

// Which icon stands for each way a project indexes itself. `off` has none:
// nothing is drawn for a project nothing reindexes, and the cell says so in a
// word instead.
const MODE_ICONS: Record<string, string> = {
  periodic: ICONS.periodic,
  auto: ICONS.auto,
};

/** The projects an organization holds, and the ones it could take.
 *
 * Nothing moves: a member keeps its own tree, its own address and its own
 * graph, and belongs to as many organizations as list it.
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
  const [taking, setTaking] = useState<string | null>(null);
  const [joining, setJoining] = useState(false);
  // A member's run reports here, and the text is written under the table:
  // these buttons are a cell, and a cell is no place for a traceback.
  const [failures, setFailures] = useState<Record<string, string>>({});
  const report = useCallback((name: string, message: string | null) => {
    setFailures((seen) => {
      if ((seen[name] ?? null) === message) {
        return seen;
      }
      const next = { ...seen };
      if (message === null) {
        delete next[name];
      } else {
        next[name] = message;
      }
      return next;
    });
  }, []);
  const members = held.data?.members ?? [];
  const names = new Set(members.map((one) => one.project));
  const free = candidates.filter(
    (one) => one.name !== project && !names.has(one.name),
  );

  return (
    <>
      <h2>Members</h2>
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
              <th title="what it is for, written on that project's own page">
                For
              </th>
              <th title="moved in, and listed here, or added and listed as its own">
                Held as
              </th>
              <th>Reads</th>
              <th title="how this member is kept indexed, and where that was set">
                Indexing
              </th>
              <th title="when it last started an index run">Indexed</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.project}>
                <td>
                  <Link to={`/projects/${encodeURIComponent(member.project)}`}>
                    {member.project}
                  </Link>
                </td>
                <td>
                  {member.description ?? (
                    <span className="muted" title="written on its own page">
                      not said
                    </span>
                  )}
                </td>
                <td>
                  <span className={member.owned ? "origin origin-db" : "muted"}>
                    {member.owned ? "moved in" : "added"}
                  </span>
                </td>
                <td className="path">{member.root_path}</td>
                <Indexing schedule={member.schedule} />
                <td>
                  {member.schedule.mode === "off" ? (
                    <span className="muted">off</span>
                  ) : (
                    <IndexedAge
                      indexedAt={member.indexed_at}
                      staleSeconds={member.stale_seconds}
                    />
                  )}
                </td>
                <td className="actions">
                  <Link
                    className="icon"
                    to={`/projects/${encodeURIComponent(member.project)}?tab=settings`}
                    title={`Settings of ${member.project}, which fall back to this organization`}
                    aria-label={`Settings of ${member.project}`}
                  >
                    <Icon path={ICONS.settings} />
                  </Link>
                  <IndexButton
                    project={member.project}
                    what={member.project}
                    compact
                    onFinished={held.reload}
                    onFailed={(message) => report(member.project, message)}
                  />
                  <button
                    type="button"
                    className="danger"
                    title={`Take ${member.project} out of ${project}; the project itself stays`}
                    aria-label={`Take ${member.project} out of ${project}`}
                    onClick={() => setTaking(member.project)}
                  >
                    <Icon path={ICONS.drop} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {Object.entries(failures).map(([name, message]) => (
        <ErrorBox
          key={name}
          message={message}
          what={`indexing ${name} failed`}
        />
      ))}

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
          onClick={() => setJoining(true)}
        >
          Add member
        </button>
      </div>

      {taking !== null && (
        <ConfirmModal
          title={`Take ${taking} out of ${project}`}
          confirmLabel="Take it out"
          danger
          onClose={() => setTaking(null)}
          onConfirm={async () => {
            await remove(`${path}/${encodeURIComponent(taking)}`);
            setTaking(null);
            held.reload();
          }}
        >
          <p>
            {taking} stops being part of {project}. The project itself is
            untouched: its tree, its graph and its own settings all stay, and it
            keeps its <code>/mcp/{taking}</code> address. If it was moved in, it
            goes back to the projects list with everything it has.
          </p>
          <ul>
            <li>A search over {project} stops reaching it.</li>
            <li>
              It stops inheriting what {project} sets, and falls back to the
              global default for anything it has not settled itself.
            </li>
          </ul>
        </ConfirmModal>
      )}

      {joining && (
        <ConfirmModal
          title={`Add ${wanted} to ${project}`}
          confirmLabel="Add it"
          onClose={() => setJoining(false)}
          onConfirm={async () => {
            await post(path, { project: wanted });
            setJoining(false);
            setWanted("");
            held.reload();
          }}
        >
          <p>
            {wanted} stays exactly where it is. Nothing is moved, copied or
            re-indexed: adding it is a reference, so it stays in the projects
            list as a project of its own.
          </p>
          <ul>
            <li>A search over {project} starts reaching it.</li>
            <li>
              It falls back to what {project} sets for anything it has not
              settled itself.
            </li>
            <li>
              While {project} lists it, it refuses to be dropped or moved into
              another project.
            </li>
          </ul>
        </ConfirmModal>
      )}

      <p className="muted">
        A member is indexed once, however many organizations hold it, and keeps
        its own <code>/mcp/&lt;name&gt;</code> address. While an organization
        lists a project, that project refuses to be dropped or moved into
        another one: take it out here first.
      </p>
    </>
  );
}

/** How one member is kept indexed, as two icons and their hover text.
 *
 * Nothing is drawn for a member in `off`: the cell beside it already says the
 * word, and an icon for "no indexing" would read as one more kind of it.
 */
function Indexing({ schedule }: { schedule: ScheduleSummary }) {
  if (schedule.mode === "off") {
    return (
      <td className="muted" title="nothing indexes it on its own">
        off
      </td>
    );
  }
  const level = SCHEDULE_LEVELS[schedule.origin] ?? schedule.origin;
  return (
    <td className="marks">
      <span title={level} aria-label={level}>
        <Icon path={ICONS.level} />
      </span>
      <span
        title={scheduleDetail(schedule)}
        aria-label={`${schedule.mode}: ${scheduleDetail(schedule)}`}
      >
        <Icon path={MODE_ICONS[schedule.mode] ?? ICONS.periodic} />
      </span>
    </td>
  );
}
