import { useEffect, useState } from "react";

import { patch } from "../api.js";
import { ErrorBox } from "./Common.js";

// The cap the route enforces, repeated here so the field says how much room
// is left rather than letting the server refuse a paragraph after typing it.
const LIMIT = 500;

/** Write what a project is for, in a sentence.
 *
 * Saved on demand rather than on every keystroke, and edited in place rather
 * than behind a modal: nothing is at stake in a sentence - no node id moves,
 * no graph is re-read - so the confirmation a type change needs would only be
 * in the way. It is read where a project is listed beside others, which is
 * what the cap is about.
 */
export function Description({
  project,
  description,
  onChanged,
}: {
  project: string;
  description: string | null;
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState(description ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // A reload, or another project opened under the same component, replaces
  // what is being edited: the draft follows the row rather than outliving it.
  useEffect(() => {
    setDraft(description ?? "");
    setError(null);
  }, [project, description]);

  const stored = description ?? "";
  const written = draft.trim();

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await patch(`/projects/${encodeURIComponent(project)}`, {
        description: written,
      });
      onChanged();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="description">
      {error !== null && <ErrorBox message={error} />}
      <label>
        What this project is for
        <textarea
          value={draft}
          rows={2}
          maxLength={LIMIT}
          placeholder={
            "One or two sentences. An organization lists this beside every " +
            "project it holds, and it is what an agent picks by."
          }
          onChange={(event) => setDraft(event.target.value)}
        />
      </label>
      <div className="row">
        <span className="muted">
          {draft.length} of {LIMIT}
        </span>
        <button
          type="button"
          disabled={busy || written === stored}
          onClick={() => void save()}
        >
          {written === "" && stored !== "" ? "Clear it" : "Save"}
        </button>
      </div>
    </div>
  );
}
