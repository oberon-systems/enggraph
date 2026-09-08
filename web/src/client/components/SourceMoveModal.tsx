import { useState } from "react";

import { post } from "../api.js";
import { ErrorBox } from "./Common.js";
import type { MoveAnswer, Project, ProjectSource } from "../types.js";

// The empty alias is a project mounted whole, and the empty string is not a
// path segment; the worker API reads `-` back as that alias.
const WHOLE = "-";

/** Name a directory suggests when it has to carry one of its own. */
function suggest(source: ProjectSource): string {
  return source.alias !== ""
    ? source.alias
    : (source.root_path.replace(/\/+$/, "").split("/").pop() ?? "");
}

/** Send one directory of a project somewhere else.
 *
 * Two destinations, one form: another project that already exists, or a
 * project this makes for it. Neither loses anything an index run does not
 * rebuild, so neither asks for the name to be typed back.
 */
export function SourceMoveModal({
  mode,
  project,
  source,
  last,
  candidates,
  types,
  onClose,
  onMoved,
}: {
  mode: "move" | "detach";
  project: string;
  source: ProjectSource;
  last: boolean;
  candidates: Project[];
  types: readonly string[];
  onClose: () => void;
  onMoved: (answer: MoveAnswer) => void;
}) {
  const [target, setTarget] = useState("");
  // The project this directory is leaving is what it is called elsewhere: the
  // alias it carries here is often empty, and never the name anyone knows it
  // by. Editable, because only the person moving it knows better.
  const [alias, setAlias] = useState(mode === "move" ? project : "");
  const [name, setName] = useState(suggest(source));
  const [type, setType] = useState("codebase");
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Moving the only directory a project has is that project moving: the row
  // goes and the records naming it follow, which is a drop by another route.
  const leaves = mode === "move" && last;
  const path =
    `/projects/${encodeURIComponent(project)}/sources/` +
    `${source.alias === "" ? WHOLE : encodeURIComponent(source.alias)}/${mode}`;
  const ready =
    (mode === "move" ? target !== "" : name.trim() !== "") &&
    (!leaves || typed === project);
  // Moving into a project that reads nothing and naming no alias mounts it
  // whole there, which is the only way an unnamed source is ever created.
  const named =
    mode === "detach"
      ? ""
      : alias.trim() ||
        (candidates.find((one) => one.name === target)?.sources.length === 0
          ? ""
          : project);

  async function send() {
    setBusy(true);
    try {
      onMoved(
        await post<MoveAnswer>(
          path,
          mode === "move"
            ? { project: target, alias }
            : { project: name.trim(), type },
        ),
      );
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <h2>
          {mode === "move" ? "Move" : "Detach"}{" "}
          {source.alias === "" ? "the whole tree" : source.alias} out of{" "}
          {project}
        </h2>
        <p>
          <code>{source.root_path}</code>{" "}
          {mode === "move"
            ? "stops being a directory of this project."
            : "becomes a project of its own, mounted whole."}
        </p>
        {mode === "move" ? (
          <>
            <label>
              Project it moves to
              <select
                value={target}
                onChange={(event) => setTarget(event.target.value)}
              >
                <option value="">choose a project</option>
                {candidates.map((one) => (
                  <option key={one.name} value={one.name}>
                    {one.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Alias it is read as there
              <input
                value={alias}
                onChange={(event) => setAlias(event.target.value)}
              />
            </label>
          </>
        ) : (
          <>
            <label>
              Name of the new project
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                autoFocus
              />
            </label>
            <label>
              Type
              <select
                value={type}
                onChange={(event) => setType(event.target.value)}
              >
                {types.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
        <p>What this costs:</p>
        <ul>
          <li>
            Node ids become{" "}
            <code>{named === "" ? "<path>" : `${named}/<path>`}</code>, so both
            projects are worth indexing again.
          </li>
          <li>What {project} built from this directory is deleted now.</li>
          {leaves ? (
            <li>
              It is the only directory {project} reads, so {project} moves
              rather than a directory of it: the project is dropped, and the
              plans, memories and suggestions naming it follow the tree.
            </li>
          ) : (
            <li>
              {project} keeps its other directories, and the plans, memories and
              suggestions naming it stay with it.
            </li>
          )}
        </ul>
        <p className="muted">
          The mount is a file on the host: run <code>make mounts</code> there
          and recreate the API before either project can be indexed.
        </p>
        {error !== null && <ErrorBox message={error} />}
        {leaves && (
          <label>
            Type <code>{project}</code> to confirm it is dropped
            <input
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoFocus
            />
          </label>
        )}
        <div className="row">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            disabled={!ready || busy}
            onClick={() => void send()}
          >
            {mode === "move" ? "Move it" : "Detach it"}
          </button>
        </div>
      </div>
    </div>
  );
}
