import { useCallback, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { get, post, query, remove } from "../api.js";
import {
  Count,
  CopyButton,
  Empty,
  ErrorBox,
  Freshness,
  Icon,
  ICONS,
  Pager,
  ScheduleBadge,
  SelectionBadge,
  Spinner,
} from "../components/Common.js";
import { AbsorbModal } from "../components/AbsorbModal.js";
import { ConfirmModal } from "../components/ConfirmModal.js";
import { SourceMoveModal } from "../components/SourceMoveModal.js";
import { TypeSelect } from "../components/TypeSelect.js";
import { DropModal } from "../components/DropModal.js";
import { Members } from "../components/Members.js";
import { GraphFrame } from "../components/GraphFrame.js";
import { IndexButton } from "../components/IndexButton.js";
import { NodeBrowser } from "../components/NodeBrowser.js";
import { isBuiltin, PROJECT_TYPES } from "./ProjectsPage.js";
import { SettingsTab } from "./SettingsTab.js";
import { useApi, useDebounced } from "../hooks/useApi.js";
import type {
  Absorbed,
  DropReport,
  FileRow,
  Memberships,
  MountedSource,
  Page,
  Project,
  ProjectDetail,
  ProjectSchedule,
  ProjectSource,
} from "../types.js";

const TABS = ["overview", "graph", "nodes", "files", "settings"] as const;
type Tab = (typeof TABS)[number];

/** Why an organization refuses to be dropped or relabelled, if it does.
 *
 * A project holding other projects is what its members point at, and one
 * holding directories is what reads them. Neither is given up as a side
 * effect of a decision about something else.
 */
function holds(project: ProjectDetail): string | null {
  if (project.type !== "organization") {
    return null;
  }
  const counted = [
    project.members > 0
      ? `${project.members} project${project.members === 1 ? "" : "s"}`
      : "",
    project.sources.length > 0
      ? `${project.sources.length} director${project.sources.length === 1 ? "y" : "ies"}`
      : "",
  ].filter((one) => one !== "");
  if (counted.length === 0) {
    return null;
  }
  return (
    `${project.name} holds ${counted.join(" and ")}. Take them out first: ` +
    "an organization that holds something stays one, and stays."
  );
}

/** The tabs a project answers anything on.
 *
 * A graph, its nodes and its files are the tree's, and neither an
 * organization nor a built-in project has one: the first holds projects and
 * the second holds what an agent wrote, both of which are read elsewhere.
 */
function tabsFor(project: ProjectDetail): readonly Tab[] {
  if (project.name.startsWith("_")) {
    return ["overview"];
  }
  if (project.type === "organization") {
    return ["overview", "settings"];
  }
  return TABS;
}

export function ProjectPage() {
  const { name = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [report, setReport] = useState<DropReport | null>(null);

  const asked = (params.get("tab") ?? "overview") as Tab;
  const detail = useApi<ProjectDetail>(`/projects/${encodeURIComponent(name)}`);

  function setParam(key: string, value: string | null) {
    const next = new URLSearchParams(params);
    if (value === null || value === "") {
      next.delete(key);
    } else {
      next.set(key, value);
    }
    setParams(next, { replace: true });
  }

  if (detail.error !== null) {
    return <ErrorBox message={detail.error} />;
  }
  if (detail.data === null) {
    return <Spinner what="the project" />;
  }
  const project = detail.data;
  const offered = tabsFor(project);
  const tab = offered.includes(asked) ? asked : "overview";
  // An organization holding anything is neither dropped nor relabelled: the
  // members point at it, and its directories are read through it. Answered
  // from what the page already knows, so the question is never asked.
  const holding = holds(project);

  return (
    <>
      <div className="row">
        <h1>{project.name}</h1>
        <button
          type="button"
          className="danger"
          onClick={() => {
            void get<DropReport>(
              `/projects/${encodeURIComponent(name)}/drop-report`,
            ).then(setReport);
          }}
        >
          Drop project
        </button>
      </div>
      <p>
        {project.name.startsWith("_") ? (
          <span className="kind">{project.type}</span>
        ) : (
          <TypeSelect
            project={project.name}
            type={project.type}
            types={PROJECT_TYPES}
            blocked={holding}
            onChanged={detail.reload}
          />
        )}{" "}
        <span className="path">{root(project)}</span>
      </p>
      <div className="row">
        <Freshness
          indexedAt={project.indexed_at}
          staleSeconds={project.stale_seconds}
        />
        {project.sources.length > 0 && (
          <IndexButton project={project.name} onFinished={detail.reload} />
        )}
      </div>
      <PartOf project={project.name} onChanged={detail.reload} />

      <nav className="tabs">
        {tabsFor(project).map((entry) => (
          <button
            key={entry}
            type="button"
            className={entry === tab ? "active" : undefined}
            onClick={() => {
              // A directory's settings are reached from its row, not carried
              // across a tab change.
              const next = new URLSearchParams(params);
              next.delete("alias");
              if (entry === "overview") {
                next.delete("tab");
              } else {
                next.set("tab", entry);
              }
              setParams(next, { replace: true });
            }}
          >
            {entry}
          </button>
        ))}
      </nav>

      {tab === "overview" && (
        <Overview
          project={project}
          onType={(type) => {
            const next = new URLSearchParams(params);
            next.set("tab", "nodes");
            next.set("type", type);
            setParams(next);
          }}
          onSources={() => {
            detail.reload();
          }}
        />
      )}
      {tab === "graph" && (
        <GraphFrame project={project.name} nodes={project.nodes} />
      )}
      {tab === "nodes" && (
        <NodeBrowser
          project={project.name}
          type={params.get("type")}
          onType={(value) => setParam("type", value)}
          selected={params.get("node")}
          onSelect={(id) => setParam("node", id)}
        />
      )}
      {tab === "files" && <FileList project={project.name} />}
      {tab === "settings" && (
        <SettingsTab
          project={project.name}
          organization={project.type === "organization"}
          alias={params.get("alias")}
          onAll={() => setParam("alias", null)}
        />
      )}

      {report !== null && (
        <DropModal
          report={report}
          blocked={holding}
          onClose={() => setReport(null)}
          onDropped={() => {
            setReport(null);
            void navigate("/");
          }}
        />
      )}
    </>
  );
}

function Overview({
  project,
  onType,
  onSources,
}: {
  project: ProjectDetail;
  onType: (type: string) => void;
  onSources: () => void;
}) {
  const listing = useApi<{ items: Project[] }>("/projects");
  const others = (listing.data?.items ?? []).filter(
    (one) => !isBuiltin(one) && one.name !== project.name,
  );
  return (
    <>
      <div className="tiles">
        {project.type === "organization" ? (
          <>
            <Tile label="Members" value={project.members} />
            <Tile label="Plans" value={project.plans} />
          </>
        ) : (
          <>
            <Tile label="Nodes" value={project.nodes} />
            <Tile label="Edges" value={project.edges} />
            <Tile label="Files" value={project.files} />
            <Tile label="Summarised" value={project.summarised} />
            <Tile label="Manual summaries" value={project.manual_summaries} />
            <Tile label="Embeddings" value={project.embeddings} />
            <Tile label="Plans" value={project.plans} />
          </>
        )}
      </div>

      {/* An organization holds projects, by reference and by nothing else:
          membership is a row, so a member keeps its tree, its mount, its node
          ids and its graph. It reads no directory of its own, which is why
          there is no table of them here. */}
      {project.type === "organization" && (
        <Members project={project.name} candidates={others} />
      )}
      {/* An organization that still reads a directory was built before this
          rule and keeps the table, which is the only way to move that
          directory back out. It cannot gain another. */}
      {(project.type !== "organization" || project.sources.length > 0) && (
        <>
          <h2>Directories</h2>
          <Directories project={project} onChanged={onSources} />
        </>
      )}

      <h2>Node types</h2>
      <div className="chips">
        {project.types.map((entry) => (
          <button
            key={entry.type}
            type="button"
            className="chip"
            onClick={() => onType(entry.type)}
          >
            {entry.type} <Count value={entry.count} />
          </button>
        ))}
      </div>

      <h2>Relations</h2>
      {project.relations.length === 0 ? (
        <Empty>No edges. Nothing in this tree resolved to anything else.</Empty>
      ) : (
        <div className="chips">
          {project.relations.map((entry) => (
            <span key={entry.relation_type} className="chip static">
              {entry.relation_type} <Count value={entry.count} />
            </span>
          ))}
        </div>
      )}

      <p className="muted">
        File hashes cover {project.hashed_files.toLocaleString("en-US")} of{" "}
        {project.files.toLocaleString("en-US")} files: they are written only for
        the parsers in the ctxgraph package, so they are not an inventory of
        what was indexed.
      </p>
      <p>
        <Link to={`/plans?project=${encodeURIComponent(project.name)}`}>
          Plans tagged with this project
        </Link>
      </p>
    </>
  );
}

/** The organizations listing this project, which is why it may refuse to go.
 *
 * Membership is a reference rather than ownership, so a project is dropped or
 * moved only once every organization has let go of it.
 */
function PartOf({
  project,
  onChanged,
}: {
  project: string;
  onChanged: () => void;
}) {
  const held = useApi<Memberships>(
    `/projects/${encodeURIComponent(project)}/organizations`,
  );
  const listing = useApi<{ items: Project[] }>("/projects");
  const [wanted, setWanted] = useState("");
  const [leaving, setLeaving] = useState<string | null>(null);
  const [joining, setJoining] = useState(false);
  const names = held.data?.organizations ?? [];
  const free = (listing.data?.items ?? []).filter(
    (one) =>
      one.type === "organization" &&
      one.name !== project &&
      !names.includes(one.name),
  );

  return (
    <>
      <p className="muted">
        {names.length === 0 ? (
          "Part of no organization. Moving it into one is a row in the " +
          "database: the tree, the mount, the node ids and the graph all " +
          "stay as they are, and nothing is indexed again."
        ) : (
          <>
            Part of{" "}
            {names.map((name, index) => (
              <span key={name}>
                {index > 0 && ", "}
                <Link to={`/projects/${encodeURIComponent(name)}`}>
                  {name}
                </Link>{" "}
                <button
                  type="button"
                  className="link"
                  title={`Take ${project} out of ${name}`}
                  onClick={() => setLeaving(name)}
                >
                  take out
                </button>
              </span>
            ))}
            . It cannot be dropped or moved into another project until it is
            taken out of {names.length > 1 ? "all of them" : "that one"}.
          </>
        )}
      </p>
      {free.length > 0 && (
        <div className="filters">
          <label>
            Move into an organization, which moves nothing
            <select
              value={wanted}
              onChange={(event) => setWanted(event.target.value)}
            >
              <option value="">choose an organization</option>
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
            Move into it
          </button>
        </div>
      )}

      {leaving !== null && (
        <ConfirmModal
          title={`Take ${project} out of ${leaving}`}
          confirmLabel="Take it out"
          danger
          onClose={() => setLeaving(null)}
          onConfirm={async () => {
            await remove(
              `/projects/${encodeURIComponent(leaving)}/members/` +
                encodeURIComponent(project),
            );
            setLeaving(null);
            held.reload();
            onChanged();
          }}
        >
          <p>
            {project} stops being part of {leaving}. Nothing here changes: the
            tree, the graph and the settings are this project's own.
          </p>
          <ul>
            <li>A search over {leaving} stops reaching this project.</li>
            <li>
              It stops inheriting what {leaving} sets, and falls back to the
              global default for anything it has not settled itself.
            </li>
          </ul>
        </ConfirmModal>
      )}

      {joining && (
        <ConfirmModal
          title={`Move ${project} into ${wanted}`}
          confirmLabel="Move it in"
          onClose={() => setJoining(false)}
          onConfirm={async () => {
            await post(`/projects/${encodeURIComponent(wanted)}/members`, {
              project,
            });
            setJoining(false);
            setWanted("");
            held.reload();
            onChanged();
          }}
        >
          <p>
            Only a row in the database changes. {project} stays exactly where it
            is: the same tree, the same mount, the same node ids, the same
            graph, and no index run.
          </p>
          <ul>
            <li>A search over {wanted} starts reaching it.</li>
            <li>
              It falls back to what {wanted} sets for anything it has not
              settled itself.
            </li>
            <li>
              While {wanted} lists it, it refuses to be dropped or moved into
              another project.
            </li>
          </ul>
        </ConfirmModal>
      )}
    </>
  );
}

/** What to print as a project's location.
 *
 * projects.root_path names a tree only when the project is one. A container
 * of named directories carries a synthetic root instead, and saying how many
 * directories it holds is the honest answer there.
 */
function root(project: ProjectDetail): string {
  const whole = project.sources.find((source) => source.alias === "");
  if (whole !== undefined) {
    return whole.root_path;
  }
  if (project.sources.length === 0) {
    return project.root_path;
  }
  const many = project.sources.length;
  return `${many} ${many === 1 ? "directory" : "directories"}`;
}

/** What a directory is called in a sentence about it. */
function named(source: ProjectSource): string {
  return source.alias === "" ? "the whole tree" : `${source.alias}/`;
}

// What puts a directory added here within reach of the services that read it.
// Neither is anything the dashboard can run: the override is a file on the
// host, and a container's mounts are fixed when it starts.
const MOUNT_COMMANDS =
  "make mounts\ndocker compose up -d --force-recreate worker-api graphify";

/** One directory's own schedule, in the shape the badge reads.
 *
 * The project's fold is not it: a directory in `off` sits in a project that
 * indexes itself every half hour, and the column exists to say so.
 */
function directorySchedule(schedule: ProjectSchedule | null, alias: string) {
  const level = schedule?.levels.find((one) => one.alias === alias);
  if (level === undefined) {
    return null;
  }
  return {
    mode: level.mode,
    interval_minutes: level.interval_minutes,
    debounce_minutes: level.debounce_minutes,
    origin: level.origins.mode ?? "default",
  };
}

/** What a project reads, and the three ways that changes.
 *
 * None of them writes a mount: the compose override is a file on the host and
 * both services hold the mounts they started with, so the API answers with what
 * finishes the job and that is shown rather than summarised.
 */
function Directories({
  project,
  onChanged,
}: {
  project: ProjectDetail;
  onChanged: () => void;
}) {
  const navigate = useNavigate();
  const [rootPath, setRootPath] = useState("");
  const [alias, setAlias] = useState("");
  const [donor, setDonor] = useState("");
  const [donorAlias, setDonorAlias] = useState("");
  const [absorbing, setAbsorbing] = useState<DropReport | null>(null);
  const [dropping, setDropping] = useState<ProjectSource | null>(null);
  const [adding, setAdding] = useState(false);
  const [absorbed, setAbsorbed] = useState<Absorbed | null>(null);
  const [sending, setSending] = useState<{
    mode: "move" | "detach";
    source: ProjectSource;
  } | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = `/projects/${encodeURIComponent(project.name)}/sources`;
  const listing = useApi<{ items: Project[] }>("/projects");
  // When each directory indexes itself, and whether this host mounts it at
  // all. Both are the API's to answer: the schedule is a fold it owns, and the
  // mounts are a directory listing the dashboard has no access to.
  const schedule = useApi<ProjectSchedule>(
    `/projects/${encodeURIComponent(project.name)}/schedule`,
  );
  const mounts = useApi<{ sources: MountedSource[] }>(path);
  const unmounted = (mounts.data?.sources ?? []).filter((one) => !one.mounted);
  // What a run of one directory reported, by alias. The buttons are a table
  // cell and the text is written under the table, which is where there is room
  // to read it.
  const [failures, setFailures] = useState<Record<string, string>>({});
  const report = useCallback((alias: string, message: string | null) => {
    setFailures((held) => {
      if ((held[alias] ?? null) === message) {
        return held;
      }
      const next = { ...held };
      if (message === null) {
        delete next[alias];
      } else {
        next[alias] = message;
      }
      return next;
    });
  }, []);
  // A project mounted whole has to name its own root before a second directory
  // can join it, and that is a host command rather than anything reachable here.
  const whole = project.sources.some((source) => source.alias === "");
  // An organization is not one of them: it holds projects rather than
  // directories, and joining one is the membership control on its own page
  // and on every project's, which moves and re-mounts nothing.
  const elsewhere = (listing.data?.items ?? []).filter(
    (other) =>
      !isBuiltin(other) &&
      other.type !== "organization" &&
      other.name !== project.name,
  );
  // A whole project is absorbed whatever shape it has; one directory can only
  // be moved into a project that is not itself mounted whole, because an
  // unnamed source and a named one cannot share a project.
  const candidates = elsewhere.filter((other) => other.sources.length > 0);
  const targets = elsewhere.filter(
    (other) => !other.sources.some((source) => source.alias === ""),
  );

  async function run(work: () => Promise<{ mounts?: string }>) {
    setError(null);
    try {
      const answer = await work();
      setHint(answer.mounts ?? null);
      onChanged();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  function pick(name: string) {
    setDonor(name);
    setDonorAlias(name);
  }

  return (
    <>
      {error !== null && <ErrorBox message={error} />}
      {unmounted.length > 0 && (
        <div className="error">
          <p>
            This host does not mount{" "}
            {unmounted.map((one) => one.alias || "the whole tree").join(", ")}.
            Nothing can read {unmounted.length === 1 ? "it" : "them"} until the
            override is rewritten and the services that hold the mounts are
            recreated:
          </p>
          <pre>{MOUNT_COMMANDS}</pre>
          <CopyButton text={MOUNT_COMMANDS} />
        </div>
      )}
      {project.sources.length === 0 ? (
        <Empty>
          This project reads no directory yet. Name one below, move another
          project in, or run{" "}
          <code>context-source {project.name} &lt;alias&gt;</code> from the
          directory itself.
        </Empty>
      ) : whole ? (
        // A project mounted whole reads one directory, which is the tree the
        // heading already names. A table of one row saying so is noise - but
        // moving that tree into another project is a thing only this row ever
        // offered, so it stays, as the sentence rather than as an icon.
        <p className="row">
          <span className="muted">
            The whole tree, indexed{" "}
            <ScheduleBadge
              schedule={directorySchedule(schedule.data, "")}
              scope="directory"
            />
            . What it selects is on the settings tab.
          </span>
          <button
            type="button"
            onClick={() =>
              setSending({ mode: "move", source: project.sources[0] })
            }
          >
            <Icon path={ICONS.move} /> Move this tree into another project
          </button>
        </p>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Alias</th>
              <th>Host path</th>
              <th title="where the last index run read the selection from">
                Selection
              </th>
              <th title="whether this directory indexes itself, and how often">
                Schedule
              </th>
              <th />
            </tr>
          </thead>
          <tbody>
            {project.sources.map((source) => (
              <tr key={source.alias}>
                <td>
                  {source.alias === "" ? (
                    <span className="muted">the whole tree</span>
                  ) : (
                    <code>{source.alias}/</code>
                  )}
                </td>
                <td className="path">{source.root_path}</td>
                <td>
                  <SelectionBadge origin={source.keep_source} />{" "}
                  <SelectionBadge origin={source.ignore_source} />
                </td>
                <td>
                  <ScheduleBadge
                    schedule={directorySchedule(schedule.data, source.alias)}
                    scope="directory"
                  />
                  {unmounted.some((one) => one.alias === source.alias) && (
                    <span className="bad"> not mounted</span>
                  )}
                </td>
                <td className="actions">
                  <Link
                    className="icon"
                    to={`/projects/${encodeURIComponent(project.name)}?tab=settings&alias=${encodeURIComponent(source.alias)}`}
                    title={`Settings of ${named(source)}: what it indexes, and when`}
                    aria-label={`Settings of ${named(source)}`}
                  >
                    <Icon path={ICONS.settings} />
                  </Link>
                  <IndexButton
                    project={project.name}
                    alias={source.alias}
                    what={named(source)}
                    compact
                    onFinished={onChanged}
                    onFailed={(message) => report(source.alias, message)}
                  />
                  <button
                    type="button"
                    title={`Move ${named(source)} to another project, which keeps reading it`}
                    aria-label={`Move ${named(source)} to another project`}
                    onClick={() => setSending({ mode: "move", source })}
                  >
                    <Icon path={ICONS.move} />
                  </button>
                  <button
                    type="button"
                    title={`Detach ${named(source)} into a project of its own`}
                    aria-label={`Detach ${named(source)} into a project of its own`}
                    onClick={() => setSending({ mode: "detach", source })}
                  >
                    <Icon path={ICONS.detach} />
                  </button>
                  {project.sources.length > 1 && (
                    <button
                      type="button"
                      className="danger"
                      title={`Stop reading ${named(source)}; the directory is left where it is`}
                      aria-label={`Stop reading ${named(source)}`}
                      onClick={() => setDropping(source)}
                    >
                      <Icon path={ICONS.drop} />
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {Object.entries(failures).map(([alias, message]) => (
        <ErrorBox
          key={alias}
          message={message}
          what={`indexing ${alias === "" ? "the whole tree" : `${alias}/`} failed`}
        />
      ))}

      <div className="filters">
        <label>
          Host path
          <input
            value={rootPath}
            placeholder="/home/you/src/mono/services/api"
            onChange={(event) => setRootPath(event.target.value)}
          />
        </label>
        <label>
          Alias, derived from the last segment when empty
          <input
            value={alias}
            onChange={(event) => setAlias(event.target.value)}
          />
        </label>
        <button
          type="button"
          disabled={rootPath === ""}
          onClick={() => setAdding(true)}
        >
          Add directory
        </button>
      </div>

      {whole ? (
        <p className="muted">
          Another project can be moved in once this one names its own root:{" "}
          <code>
            make source-promote PROJECT_NAME={project.name} ALIAS=&lt;alias&gt;
          </code>{" "}
          on the host, then index it again.
        </p>
      ) : (
        <div className="filters">
          <label>
            Move a project in, which drops it
            <select
              value={donor}
              onChange={(event) => pick(event.target.value)}
            >
              <option value="">choose a project</option>
              {candidates.map((other) => (
                <option key={other.name} value={other.name}>
                  {other.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Alias its tree is read as
            <input
              value={donorAlias}
              onChange={(event) => setDonorAlias(event.target.value)}
            />
          </label>
          <button
            type="button"
            disabled={donor === ""}
            onClick={() =>
              void run(async () => {
                setAbsorbing(
                  await get<DropReport>(
                    `/projects/${encodeURIComponent(donor)}/drop-report`,
                  ),
                );
                return {};
              })
            }
          >
            Move in
          </button>
        </div>
      )}

      {hint !== null && <p className="stale">{hint}</p>}

      {absorbed !== null && (
        <p className="muted">
          <Count value={absorbed.plans} /> plans,{" "}
          <Count value={absorbed.memories} /> memories and{" "}
          <Count value={absorbed.suggestions} /> suggestions now name{" "}
          {project.name}.
        </p>
      )}

      {project.sources.length > 1 && (
        <p className="muted">
          Each alias opens every node id that directory produced, so a file of
          the first slice is <code>{project.sources[0].alias}/...</code> in the
          graph. The last directory cannot be dropped: a project with no tree is
          dropped itself.
        </p>
      )}

      {dropping !== null && (
        <ConfirmModal
          title={`Stop ${project.name} reading ${named(dropping)}`}
          confirmLabel="Stop reading it"
          danger
          onClose={() => setDropping(null)}
          onConfirm={async () => {
            const answer = await remove<{ mounts?: string }>(
              `${path}/${encodeURIComponent(dropping.alias)}`,
            );
            setDropping(null);
            setHint(answer.mounts ?? null);
            onChanged();
          }}
        >
          <p>
            <code>{dropping.root_path}</code> stops being one of the directories{" "}
            {project.name} reads.
          </p>
          <ul>
            <li>
              The directory on the host is untouched: every mount here is
              read-only.
            </li>
            <li>
              The nodes it produced stay until the next index run of{" "}
              {project.name} prunes them.
            </li>
            <li>
              What it selects - its <code>.ctxkeep</code> and{" "}
              <code>.ctxignore</code> rows - is deleted now, because a row for a
              directory nothing reads decides nothing, and would decide again if
              the alias ever came back.
            </li>
          </ul>
          <p className="muted">
            Adding it back is that host path and this alias, in the form below.
          </p>
        </ConfirmModal>
      )}

      {adding && (
        <ConfirmModal
          title={`Read ${rootPath} as part of ${project.name}`}
          confirmLabel="Add the directory"
          onClose={() => setAdding(false)}
          onConfirm={async () => {
            const answer = await post<{ mounts?: string }>(path, {
              root_path: rootPath,
              alias,
            });
            setAdding(false);
            setRootPath("");
            setAlias("");
            setHint(answer.mounts ?? null);
            onChanged();
          }}
        >
          <p>
            Every node id it produces will carry{" "}
            <code>{alias.trim() || "the last segment of that path"}</code> as
            its first segment.
          </p>
          <p className="muted">
            Nothing is mounted by this: run <code>make mounts</code> on the host
            and recreate the API, then index it.
          </p>
        </ConfirmModal>
      )}

      {sending !== null && (
        <SourceMoveModal
          mode={sending.mode}
          project={project.name}
          source={sending.source}
          last={project.sources.length === 1}
          candidates={targets}
          types={PROJECT_TYPES}
          onClose={() => setSending(null)}
          onMoved={(answer) => {
            setSending(null);
            // The last directory leaving takes the project with it, so this
            // page is about a name that no longer exists: follow the tree.
            if (answer.moved.dropped) {
              void navigate(
                `/projects/${encodeURIComponent(answer.moved.project)}`,
              );
              return;
            }
            setAbsorbed(null);
            setHint(answer.mounts ?? null);
            onChanged();
          }}
        />
      )}

      {absorbing !== null && (
        <AbsorbModal
          target={project.name}
          alias={donorAlias}
          report={absorbing}
          onClose={() => setAbsorbing(null)}
          onAbsorbed={(answer) => {
            setAbsorbing(null);
            setAbsorbed(answer.absorbed);
            setHint(answer.mounts ?? null);
            setDonor("");
            setDonorAlias("");
            onChanged();
          }}
        />
      )}
    </>
  );
}

function Tile({ label, value }: { label: string; value: number }) {
  return (
    <div className="tile">
      <span className="tile-value">{value.toLocaleString("en-US")}</span>
      <span className="tile-label">{label}</span>
    </div>
  );
}

function FileList({ project }: { project: string }) {
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const debounced = useDebounced(search);
  const { data, error, loading } = useApi<Page<FileRow>>(
    `/projects/${encodeURIComponent(project)}/files${query({
      q: debounced,
      limit: 50,
      offset,
    })}`,
  );

  return (
    <>
      <input
        className="search"
        placeholder="Search paths"
        value={search}
        onChange={(event) => {
          setSearch(event.target.value);
          setOffset(0);
        }}
      />
      {error !== null && <ErrorBox message={error} />}
      {loading && data === null && <Spinner what="files" />}
      {data !== null && data.items.length === 0 && (
        <Empty>No file matches that.</Empty>
      )}
      {data !== null && data.items.length > 0 && (
        <>
          <table className="grid">
            <thead>
              <tr>
                <th>Path</th>
                <th className="num">Entities</th>
                <th>Summary</th>
                <th>Hashed</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((file) => (
                <tr key={file.id}>
                  <td className="path">{file.id}</td>
                  <td className="num">
                    <Count value={file.entities} />
                  </td>
                  <td className="summary">{file.summary}</td>
                  <td>
                    {file.hash === null ? (
                      <span className="muted">no</span>
                    ) : (
                      "yes"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager
            total={data.total}
            limit={data.limit}
            offset={data.offset}
            onOffset={setOffset}
          />
        </>
      )}
    </>
  );
}
