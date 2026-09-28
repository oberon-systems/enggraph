import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { post, query } from "../api.js";
import { Empty, ErrorBox, Spinner } from "../components/Common.js";
import { useApi } from "../hooks/useApi.js";
import type { Page, ProjectListing, SkillRow } from "../types.js";

const ALL = "*";
const GLOBAL = "_global";

export function SkillsPage() {
  const [params, setParams] = useSearchParams();
  const owner = params.get("owner") ?? ALL;
  const skills = useApi<SkillRow[]>(`/skills${query({ owner })}`);
  const projects = useApi<Omit<Page<ProjectListing>, "total">>("/projects");
  const [importing, setImporting] = useState(false);

  const owners = (projects.data?.items ?? [])
    .filter((project) => !project.name.startsWith("_"))
    .map((project) => project.name);

  function setOwner(value: string) {
    const next = new URLSearchParams(params);
    if (value === ALL) {
      next.delete("owner");
    } else {
      next.set("owner", value);
    }
    setParams(next);
  }

  return (
    <>
      <div className="row">
        <h1>Skills</h1>
        <button type="button" onClick={() => setImporting(!importing)}>
          {importing ? "Close" : "Import"}
        </button>
      </div>
      <p className="muted">
        What the MCP server hands to agents. Built-in skills come from{" "}
        <code>skills/</code> of this repository and are read-only; imported ones
        belong to everyone, an organization or a project. Which ones a project
        gets is switched on its Skills tab.
      </p>

      {importing && (
        <ImportForm
          owners={owners}
          onDone={() => {
            setImporting(false);
            skills.reload();
          }}
        />
      )}

      <div className="filters">
        <label>
          Owner
          <select
            value={owner}
            onChange={(event) => setOwner(event.target.value)}
          >
            <option value={ALL}>every scope</option>
            <option value={GLOBAL}>global</option>
            {owners.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {skills.error !== null && <ErrorBox message={skills.error} />}
      {skills.loading && skills.data === null && <Spinner what="skills" />}
      {skills.data !== null && skills.data.length === 0 && (
        <Empty>No skill here yet.</Empty>
      )}
      {skills.data !== null && skills.data.length > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th>Skill</th>
              <th>Owner</th>
              <th>Source</th>
              <th>Version</th>
              <th title="characters of text">Size</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {skills.data.map((row) => (
              <tr key={row.id}>
                <td>
                  <Link to={`/skills/${row.id}`}>{row.name}</Link>
                </td>
                <td>
                  {row.owner ?? <span className="chip static">global</span>}
                </td>
                <td>
                  <span className="chip static">
                    {row.source === "repo" ? "built-in" : "imported"}
                  </span>
                </td>
                <td className="muted id">{row.sha256.slice(0, 12)}</td>
                <td className="num">{row.length}</td>
                <td className="muted">{row.updated_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function ImportForm({
  owners,
  onDone,
}: {
  owners: string[];
  onDone: () => void;
}) {
  const [owner, setOwner] = useState(GLOBAL);
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function load(file: File | undefined) {
    if (file !== undefined) {
      setContent(await file.text());
    }
  }

  async function save() {
    try {
      await post("/skills", { owner, content });
      setError(null);
      onDone();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <>
      <div className="filters">
        <label>
          Import into
          <select
            value={owner}
            onChange={(event) => setOwner(event.target.value)}
          >
            <option value={GLOBAL}>global</option>
            {owners.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          SKILL.md
          <input
            type="file"
            accept=".md,text/markdown"
            onChange={(event) => void load(event.target.files?.[0])}
          />
        </label>
      </div>
      <textarea
        className="editor"
        placeholder={"---\nname: alpha\ndescription: ...\n---\n"}
        value={content}
        onChange={(event) => setContent(event.target.value)}
      />
      {error !== null && <ErrorBox message={error} />}
      <div className="row">
        <button
          type="button"
          disabled={content.trim() === ""}
          onClick={() => void save()}
        >
          Import
        </button>
      </div>
    </>
  );
}
