import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { get, post, put, query, remove } from "../api.js";
import {
  Count,
  Coverage,
  Empty,
  ErrorBox,
  Freshness,
  Pager,
  Spinner,
} from "../components/Common.js";
import { ConfirmModal } from "../components/ConfirmModal.js";
import { Description } from "../components/Description.js";
import { TypeSelect } from "../components/TypeSelect.js";
import { DropModal } from "../components/DropModal.js";
import { RenameModal } from "../components/RenameModal.js";
import { Members } from "../components/Members.js";
import { GraphFrame } from "../components/GraphFrame.js";
import { IndexButton } from "../components/IndexButton.js";
import { NodeBrowser } from "../components/NodeBrowser.js";
import { isBuiltin, PROJECT_TYPES } from "./ProjectsPage.js";
import { SettingsTab } from "./SettingsTab.js";
import { useApi, useDebounced } from "../hooks/useApi.js";
import type {
  DropReport,
  EmbeddingsView,
  FileRow,
  Memberships,
  Page,
  Project,
  ProjectDetail,
  SummariesView,
} from "../types.js";

const TABS = ["overview", "graph", "nodes", "files", "settings"] as const;
type Tab = (typeof TABS)[number];

/** Why an organization refuses to be dropped or relabelled, if it does.
 *
 * A project holding other projects is what its members point at, and that is
 * not given up as a side effect of a decision about something else.
 */
function holds(project: ProjectDetail): string | null {
  if (project.type !== "organization" || project.members === 0) {
    return null;
  }
  return (
    `${project.name} holds ${project.members} ` +
    `project${project.members === 1 ? "" : "s"}. Take them out first: an ` +
    "organization that holds something stays one, and stays."
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
  const [renaming, setRenaming] = useState(false);

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
  // members point at it. Answered from what the page already knows, so the
  // question is never asked.
  const holding = holds(project);

  return (
    <>
      <div className="row">
        <h1>{project.name}</h1>
        {!isBuiltin(project) && (
          <button type="button" onClick={() => setRenaming(true)}>
            Rename
          </button>
        )}
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
      {renaming && (
        <RenameModal
          project={project.name}
          onClose={() => setRenaming(false)}
          onRenamed={(renamed) => {
            setRenaming(false);
            // The page this was opened on is gone: the row answers under the
            // new name and the old URL is a 404.
            void navigate(`/projects/${encodeURIComponent(renamed)}`);
          }}
        />
      )}
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
        <span className="path">{project.root_path}</span>
      </p>
      {!project.name.startsWith("_") && (
        <Description
          project={project.name}
          description={project.description}
          onChanged={detail.reload}
        />
      )}
      <div className="row">
        <Freshness
          indexedAt={project.indexed_at}
          staleSeconds={project.stale_seconds}
        />
        {project.type !== "organization" && (
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
              const next = new URLSearchParams(params);
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
}: {
  project: ProjectDetail;
  onType: (type: string) => void;
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

      {project.type !== "organization" && <Queues project={project.name} />}

      {/* An organization holds projects, by reference and by nothing else:
          membership is a row, so a member keeps its tree, its mount, its node
          ids and its graph. It reads no directory of its own, which is why
          there is no table of them here. */}
      {project.type === "organization" && (
        <Members project={project.name} candidates={others} />
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
        the parsers in the enggraph package, so they are not an inventory of
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
  const [joining, setJoining] = useState<"add" | "move" | null>(null);
  const names = held.data?.organizations ?? [];
  // The organization it was moved into, if it was: that one holds it, and it
  // is why the project is not listed beside the others.
  const owner = held.data?.owner ?? null;
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
            {owner === null ? "Added to " : "Moved into "}
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
            .{" "}
            {owner === null
              ? "It is a project of its own, listed with the others, and " +
                "belongs to as many organizations as are relevant to it."
              : `${owner} is where it is listed, so it is not in the projects ` +
                "list. Taking it out puts it back, with everything it has: " +
                "the same tree, the same mount and the same graph."}
          </>
        )}
      </p>
      {free.length > 0 && (
        <div className="filters">
          <label>
            An organization, to add this project to or move it into
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
          {/* Two things, and the difference is what happens to the
              organizations already holding it: adding is one more of them,
              moving is this one instead of them. Neither moves a file. */}
          <button
            type="button"
            disabled={wanted === ""}
            onClick={() => setJoining("add")}
          >
            Add to it
          </button>
          <button
            type="button"
            disabled={wanted === "" || names.length > 0}
            title={
              names.length > 0
                ? `${project} is part of ${names.join(", ")}: take it out ` +
                  "there first, or add it to this one as well"
                : `Move ${project} into this organization`
            }
            onClick={() => setJoining("move")}
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

      {joining !== null && (
        <ConfirmModal
          title={
            joining === "add"
              ? `Add ${project} to ${wanted}`
              : `Move ${project} into ${wanted}`
          }
          confirmLabel={joining === "add" ? "Add it" : "Move it in"}
          onClose={() => setJoining(null)}
          onConfirm={async () => {
            // Adding is one more row; moving is where it belongs settled
            // outright, so the organizations it leaves go in the same call
            // rather than one request per name.
            if (joining === "add") {
              await post(`/projects/${encodeURIComponent(wanted)}/members`, {
                project,
              });
            } else {
              await put(
                `/projects/${encodeURIComponent(project)}/organizations`,
                {
                  organizations: [wanted],
                },
              );
            }
            setJoining(null);
            setWanted("");
            held.reload();
            onChanged();
          }}
        >
          <p>
            Only rows in the database change. {project} stays exactly where it
            is: the same tree, the same mount, the same node ids, the same
            graph, and no index run.
          </p>
          <ul>
            {joining === "move" ? (
              <li>
                {wanted} becomes where it is listed, so it leaves the projects
                list. Taking it out there puts it back.
              </li>
            ) : (
              <li>
                It stays in the projects list: adding it is a reference, and it
                belongs to as many organizations as are relevant to it.
              </li>
            )}
            <li>A search over {wanted} starts reaching it.</li>
            <li>
              It falls back to what {wanted} sets for anything it has not
              settled itself.
            </li>
            {joining === "add" && names.length > 0 && (
              <li>
                It stays part of {names.join(", ")} as well: a project belongs
                to as many organizations as list it.
              </li>
            )}
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

/** How far the two model queues have got with this project.
 *
 * The same two numbers the Queues page shows for every project at once, on
 * the page of the one being looked at. Files described by hand are left out
 * of the total rather than counted as done: the model is forbidden from
 * touching them, so a percent that counted them could never reach 100 and
 * would read as broken.
 */
function Queues({ project }: { project: string }) {
  const summaries = useApi<SummariesView>("/summaries");
  const embeddings = useApi<EmbeddingsView>("/embeddings");
  const summary = summaries.data?.summaries.find(
    (one) => one.project === project,
  );
  const embedding = embeddings.data?.embeddings.find(
    (one) => one.project === project,
  );
  if (summary === undefined && embedding === undefined) {
    return null;
  }
  return (
    <p className="muted">
      Summarised{" "}
      {summary === undefined ? (
        "-"
      ) : (
        <Coverage
          done={summary.described}
          total={Math.max(0, summary.files - summary.manual)}
          muted={!summary.enabled}
          title={
            summary.manual === 0
              ? undefined
              : `${summary.manual} file(s) written by hand are left out`
          }
        />
      )}
      {" - embedded "}
      {embedding === undefined ? (
        "-"
      ) : (
        <Coverage
          done={embedding.files}
          total={embedding.indexed_files}
          muted={!embedding.enabled}
          title={`${embedding.chunks} chunk(s) written`}
        />
      )}
      {" - "}
      <Link to="/queues">both queues</Link>
    </p>
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
