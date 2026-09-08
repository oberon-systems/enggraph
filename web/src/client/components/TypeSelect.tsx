import { useState } from "react";

import { patch } from "../api.js";
import { ErrorBox } from "./Common.js";

/** Change a project's type, once the change is confirmed by name.
 *
 * The select alone used to write it: one stray scroll over the control
 * retyped a project, and a rejected request said nothing at all. Picking a
 * value now only opens the question.
 */
export function TypeSelect({
  project,
  type,
  types,
  onChanged,
}: {
  project: string;
  type: string;
  types: readonly string[];
  onChanged: () => void;
}) {
  const [wanted, setWanted] = useState<string | null>(null);

  return (
    <>
      <select
        value={wanted ?? type}
        onChange={(event) => setWanted(event.target.value)}
      >
        {[...new Set([type, ...types])].map((one) => (
          <option key={one} value={one}>
            {one}
          </option>
        ))}
      </select>
      {wanted !== null && wanted !== type && (
        <TypeModal
          project={project}
          type={type}
          wanted={wanted}
          onClose={() => setWanted(null)}
          onChanged={() => {
            setWanted(null);
            onChanged();
          }}
        />
      )}
    </>
  );
}

function TypeModal({
  project,
  type,
  wanted,
  onClose,
  onChanged,
}: {
  project: string;
  type: string;
  wanted: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function apply() {
    setBusy(true);
    setError(null);
    try {
      await patch(`/projects/${encodeURIComponent(project)}`, { type: wanted });
      onChanged();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <h2>Change the type of {project}</h2>
        <p>
          <code>{type}</code> becomes <code>{wanted}</code>.
        </p>
        <p className="muted">
          A type says what a project is, not what is indexed in it: nothing is
          re-read, no node id changes, and the graph is untouched. It is the
          filter the MCP tools search by, so it decides which projects a
          question about codebases, docs or one organization reaches.
        </p>
        {error !== null && <ErrorBox message={error} />}
        <label>
          Type <code>{project}</code> to confirm
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoFocus
          />
        </label>
        <div className="row">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            disabled={typed !== project || busy}
            onClick={() => void apply()}
          >
            Change the type
          </button>
        </div>
      </div>
    </div>
  );
}
