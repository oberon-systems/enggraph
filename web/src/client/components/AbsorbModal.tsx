import { useState } from "react";

import { post } from "../api.js";
import { Count, ErrorBox } from "./Common.js";
import type { AbsorbAnswer, DropReport } from "../types.js";

/** Confirm folding one project into another, which drops the one that moves.
 *
 * The drop report is the donor's, so its counts say what the graph loses; what
 * the target gains is one directory it has to be indexed for.
 */
export function AbsorbModal({
  target,
  alias,
  report,
  onClose,
  onAbsorbed,
}: {
  target: string;
  alias: string;
  report: DropReport;
  onClose: () => void;
  onAbsorbed: (answer: AbsorbAnswer) => void;
}) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const named = alias.trim() === "" ? report.name : alias.trim();

  async function absorb() {
    setBusy(true);
    try {
      const answer = await post<AbsorbAnswer>(
        `/projects/${encodeURIComponent(target)}/absorb`,
        { project: report.name, alias },
      );
      onAbsorbed(answer);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <h2>
          Move {report.name} into {target}
        </h2>
        <p>
          <code>{report.root_path}</code> becomes a directory of {target}, read
          as <code>{named}/</code>, and {report.name} stops existing.
        </p>
        <p>Moved with it:</p>
        <ul>
          <li>
            <Count value={report.plans} /> plans
          </li>
          <li>
            <Count value={report.suggestions} /> suggestions
          </li>
        </ul>
        <p>One index run of {target} rebuilds:</p>
        <ul>
          <li>
            <Count value={report.nodes} /> nodes
          </li>
          <li>
            <Count value={report.edges} /> edges
          </li>
          <li>
            <Count value={report.hashes} /> file hashes
          </li>
          <li>
            <Count value={report.embeddings} /> embeddings
          </li>
        </ul>
        <p>Nothing rebuilds:</p>
        <ul>
          <li>
            <Count value={report.summaries} /> manual summaries
          </li>
        </ul>
        <p className="muted">
          Every node id gains <code>{named}/</code> as its first segment, and
          nothing rewrites ids: the summaries were written against the ones this
          drops.
        </p>
        {error !== null && <ErrorBox message={error} />}
        <label>
          Type <code>{report.name}</code> to confirm
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
            className="danger"
            disabled={typed !== report.name || busy}
            onClick={() => void absorb()}
          >
            Move it in
          </button>
        </div>
      </div>
    </div>
  );
}
