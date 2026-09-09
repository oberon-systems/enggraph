import { useState } from "react";

import { post } from "../api.js";
import { ErrorBox } from "./Common.js";

/** Give a project another name, confirmed by typing the one it has.
 *
 * A rename is rows and nothing else - the graph, the directories, the settings
 * and the memberships are re-keyed where they stand, and nothing is read
 * again. What it does break is every address built from the name, and that is
 * what the confirmation is for: the mount, and the `.mcp.json` of the codebase
 * that was onboarded against it.
 */
export function RenameModal({
  project,
  onClose,
  onRenamed,
}: {
  project: string;
  onClose: () => void;
  // Given the new name: the page it was opened on no longer exists.
  onRenamed: (name: string) => void;
}) {
  const [wanted, setWanted] = useState(project);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const asked = wanted.trim();
  const ready = typed === project && asked !== "" && asked !== project && !busy;

  async function apply() {
    setBusy(true);
    setError(null);
    try {
      const answer = await post<{ renamed?: { project?: string } }>(
        `/projects/${encodeURIComponent(project)}/rename`,
        { project: asked },
      );
      // The server cleans the name - lowercased, and anything a URL path
      // segment would not carry replaced - so the page follows what it
      // stored rather than what was typed.
      onRenamed(answer?.renamed?.project ?? asked);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <h2>Rename {project}</h2>
        <p className="muted">
          Rows and nothing else: the graph, the directories, the settings and
          the memberships keep everything they hold, and no tree is read again.
          A node id is relative to a directory rather than to the project, so
          not one of them changes. The plans, memories and suggestions written
          about the old name follow it.
        </p>
        <ul>
          <li>
            Its address becomes <code>/mcp/{asked || "<name>"}</code>. Any
            codebase onboarded against <code>/mcp/{project}</code> keeps
            pointing at the old one until its <code>.mcp.json</code> is changed.
          </li>
          <li>
            The mount is a file on the host: run <code>make mounts</code> and
            restart the services, or the next index run has nothing to read.
          </li>
        </ul>
        {error !== null && <ErrorBox message={error} />}
        <label>
          New name
          <input
            value={wanted}
            onChange={(event) => setWanted(event.target.value)}
            autoFocus
          />
        </label>
        <label>
          Type <code>{project}</code> to confirm
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
          />
        </label>
        <div className="row">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" disabled={!ready} onClick={() => void apply()}>
            Rename it
          </button>
        </div>
      </div>
    </div>
  );
}
