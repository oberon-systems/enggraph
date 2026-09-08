import { useState } from "react";

import { ErrorBox } from "./Common.js";

/** Ask before doing something, and say what it will do.
 *
 * Every control that changes stored state goes through this. An icon is quick
 * to press and says nothing on its own, so what it is about to do is stated
 * here instead - and a refusal from the API is shown in place rather than
 * leaving the page looking as though nothing happened.
 */
export function ConfirmModal({
  title,
  confirmLabel,
  danger = false,
  children,
  onConfirm,
  onClose,
}: {
  title: string;
  confirmLabel: string;
  danger?: boolean;
  children: React.ReactNode;
  onConfirm: () => Promise<unknown>;
  onClose: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function go() {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <h2>{title}</h2>
        {children}
        {error !== null && <ErrorBox message={error} />}
        <div className="row">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className={danger ? "danger" : undefined}
            disabled={busy}
            onClick={() => void go()}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
