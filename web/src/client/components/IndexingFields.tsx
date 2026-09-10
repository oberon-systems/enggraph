import { useState } from "react";

import { put } from "../api.js";
import { ErrorBox } from "./Common.js";
import { Switch } from "./Switch.js";
import type {
  Feature,
  FeatureState,
  Indexing,
  ScheduleSummary,
} from "../types.js";

// What a schedule may say, as enggraph.config spells it, with what each mode
// means where the reader is - a select showing three words explains nothing.
const MODES: [string, string][] = [
  ["off", "off - indexed when asked, and not otherwise"],
  ["periodic", "periodic - every N minutes"],
  ["auto", "auto - when a file changes, and every N minutes regardless"],
];

// What an empty field means. The global level has no level above it, so what
// it falls back to is the built-in default rather than another row.
const INHERIT = "inherit from the level above";
const BUILT_IN = "the built-in default: off, every 60 minutes";

// The three fields as typed rather than as stored: empty is how a level
// declines to answer, and that is a state a number cannot hold.
export type IndexingDraft = {
  mode: string;
  interval: string;
  debounce: string;
};

export function draftOf(indexing: Indexing | undefined): IndexingDraft {
  return {
    mode: indexing?.mode ?? "",
    interval: indexing?.interval_minutes?.toString() ?? "",
    debounce: indexing?.debounce_minutes?.toString() ?? "",
  };
}

/** Turn a draft into a request body, an empty field meaning "not mine to say". */
export function bodyOf(draft: IndexingDraft) {
  return {
    mode: draft.mode,
    interval_minutes: draft.interval === "" ? null : Number(draft.interval),
    debounce_minutes: draft.debounce === "" ? null : Number(draft.debounce),
  };
}

export const EMPTY: IndexingDraft = { mode: "", interval: "", debounce: "" };

export function IndexingFields({
  draft,
  onChange,
  root = false,
  settled,
}: {
  draft: IndexingDraft;
  onChange: (draft: IndexingDraft) => void;
  root?: boolean;
  // What these fields come to once every level is folded in. Shown as the
  // placeholder, because a box saying "default" answers nothing.
  settled?: ScheduleSummary;
}) {
  const empty = root ? BUILT_IN : INHERIT;
  const inForce = (value: number | undefined) =>
    value === undefined ? (root ? "default" : "inherit") : String(value);
  return (
    <div className="filters">
      <label>
        When it indexes
        <select
          value={draft.mode}
          onChange={(event) => onChange({ ...draft, mode: event.target.value })}
        >
          <option value="">{empty}</option>
          {MODES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Every, minutes
        <input
          type="number"
          min={1}
          max={10080}
          placeholder={inForce(settled?.interval_minutes)}
          value={draft.interval}
          onChange={(event) =>
            onChange({ ...draft, interval: event.target.value })
          }
        />
      </label>
      <label>
        And no more often than, minutes
        <input
          type="number"
          min={1}
          max={1440}
          placeholder={inForce(settled?.debounce_minutes)}
          value={draft.debounce}
          onChange={(event) =>
            onChange({ ...draft, debounce: event.target.value })
          }
        />
      </label>
    </div>
  );
}

/** One level's schedule: the switch that gates it, its fields, and one Save.
 *
 * The switch lives here rather than in a block of its own, because a block
 * saying "whether this project indexes itself" beside a block saying "how
 * often" is two headings about one decision.
 *
 * "Inherit" saves an empty draft rather than deleting a row: the same request
 * clears the key, and a level with nothing to say is a level that inherits.
 */
export function IndexingEditor({
  path,
  indexing,
  feature,
  settled,
  schedule,
  onSaved,
  root = false,
}: {
  path: string;
  indexing: Indexing | undefined;
  feature: Feature | undefined;
  settled?: FeatureState;
  // The schedule as resolved, for the placeholders in the two number boxes.
  schedule?: ScheduleSummary;
  onSaved: () => void;
  root?: boolean;
}) {
  const [draft, setDraft] = useState<IndexingDraft>(draftOf(indexing));
  const [enabled, setEnabled] = useState<boolean | null>(
    feature?.enabled ?? null,
  );
  const [allowed, setAllowed] = useState<boolean>(feature?.allowed ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inherited = enabled === null;
  const shown = enabled ?? settled?.enabled ?? true;
  const gated = settled?.allowed === false && !root;

  /** Clear this level rather than storing an empty answer, as above. */
  async function clear() {
    setBusy(true);
    setError(null);
    try {
      await put(path, {});
      setDraft(EMPTY);
      setEnabled(null);
      setAllowed(true);
      onSaved();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function save(value: IndexingDraft, switched: boolean | null) {
    setBusy(true);
    setError(null);
    try {
      // One request, not two: the switch lives in the same settings object as
      // the schedule, and that object is replaced rather than merged - so a
      // second write would drop whatever the first one had just stored.
      await put(path, { ...bodyOf(value), enabled: switched, allowed });
      setDraft(value);
      setEnabled(switched);
      onSaved();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {error !== null && <ErrorBox message={error} />}
      <div className="feature-row">
        {root && (
          <Switch
            checked={allowed}
            label={allowed ? "enabled" : "disabled"}
            title="whether anything may index a project without being asked"
            onChange={setAllowed}
          />
        )}
        <Switch
          checked={shown}
          inherited={inherited && !root}
          disabled={gated}
          label={
            gated
              ? "disabled globally"
              : inherited && !root
                ? `inherited: ${shown ? "on" : "off"}`
                : `status: ${shown ? "on" : "off"}`
          }
          title={
            gated
              ? "indexing is disabled, so this level is not asked at all"
              : root
                ? "what a project that says nothing about itself does"
                : "whether anything indexes this without being asked"
          }
          onChange={setEnabled}
        />
      </div>
      <IndexingFields
        draft={draft}
        onChange={setDraft}
        root={root}
        settled={schedule}
      />
      <div className="row">
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() => void clear()}
        >
          {root ? "Back to the built-in defaults" : "Inherit everything"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void save(draft, enabled)}
        >
          Save
        </button>
      </div>
    </>
  );
}
