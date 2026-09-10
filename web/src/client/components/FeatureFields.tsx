import { useState } from "react";

import { post, put } from "../api.js";
import { ErrorBox } from "./Common.js";
import { Switch } from "./Switch.js";
import type { Feature, FeatureState, InheritedFeature } from "../types.js";

// What one feature's row holds while it is being edited. `enabled` is
// null while this level says nothing and the position shown was decided
// above - the state a checkbox cannot hold, kept beside it instead.
export type FeatureDraft = {
  // Whether the feature may run anywhere. Only the global page shows it, and
  // only the global level stores it.
  allowed: boolean;
  enabled: boolean | null;
  // How fast the queue is worked, as typed: empty is this level declining to
  // say, which a number cannot hold.
  batch: string;
  tick: string;
  budget: string;
  chunkChars: string;
  chunkOverlap: string;
  url: string;
  // Write-only, and empty on every load: a stored token never travels back to
  // a browser, so this is what replaces one rather than what shows it.
  key: string;
};

export function draftOf(feature: Feature | undefined): FeatureDraft {
  return {
    allowed: feature?.allowed ?? true,
    enabled: feature?.enabled ?? null,
    batch: feature?.batch?.toString() ?? "",
    tick: feature?.tick_seconds?.toString() ?? "",
    budget: feature?.budget_seconds?.toString() ?? "",
    chunkChars: feature?.chunk_chars?.toString() ?? "",
    chunkOverlap: feature?.chunk_overlap?.toString() ?? "",
    url: feature?.server_url ?? "",
    key: "",
  };
}

/** What an empty box means: the value it inherits, and the level it comes from.
 *
 * A field showing "inherit" tells a reader nothing they wanted to know, so the
 * placeholder is the value an empty box actually gets, resolved above it.
 */
function inherits(
  value: number | string | undefined,
  origin: string | undefined,
): string {
  if (value === undefined || value === "") {
    return "inherited: nothing set";
  }
  if (origin === "default" || origin === "environment") {
    return `${origin}: ${value}`;
  }
  return origin === undefined
    ? `inherited: ${value}`
    : `inherited from ${origin}: ${value}`;
}

type Placed = Exclude<keyof InheritedFeature, "enabled" | "origins">;

/** Which server this level dials, and whether it chose it or inherited it. */
function urlInForce(settled: FeatureState | undefined, own: boolean): string {
  const url = settled?.server_url || settled?.inherited?.server_url || "";
  if (url === "") {
    return "No server URL in force: nothing is dialled from here.";
  }
  if (own) {
    return `URL in force: ${url} (set on this level)`;
  }
  const origin = settled?.server_url
    ? settled.origins.server_url
    : settled?.inherited?.origins.server_url;
  return `URL in force: ${url} (inherited from ${origin ?? "the level above"})`;
}

/** Turn a draft into a request body, an empty field meaning "not mine to say". */
export function bodyOf(draft: FeatureDraft) {
  return {
    allowed: draft.allowed,
    enabled: draft.enabled,
    server_url: draft.url.trim(),
    server_key: draft.key.trim(),
    batch: draft.batch === "" ? null : Number(draft.batch),
    tick_seconds: draft.tick === "" ? null : Number(draft.tick),
    budget_seconds: draft.budget === "" ? null : Number(draft.budget),
    chunk_chars: draft.chunkChars === "" ? null : Number(draft.chunkChars),
    chunk_overlap:
      draft.chunkOverlap === "" ? null : Number(draft.chunkOverlap),
  };
}

export const EMPTY: FeatureDraft = {
  allowed: true,
  enabled: null,
  batch: "",
  tick: "",
  budget: "",
  chunkChars: "",
  chunkOverlap: "",
  url: "",
  key: "",
};

/**
 * One feature: a switch, the server it dials, and one button.
 *
 * The button is Test while an address has been typed that nobody has dialled
 * yet, and Save once it has answered. That order is the point: an address
 * that is stored without answering is found out by a queue going quiet an
 * hour later, which is the failure this row exists to prevent.
 */
export function FeatureEditor({
  path,
  probePath,
  feature,
  settled,
  onSaved,
  root = false,
  withUrl = true,
  children,
}: {
  path: string;
  // Where to dial an address before storing it. Omitted for a feature that
  // has no server of its own.
  probePath?: string;
  feature: Feature | undefined;
  settled?: FeatureState;
  onSaved: () => void;
  root?: boolean;
  withUrl?: boolean;
  // Fields belonging to this feature, rendered between the switch and the
  // button - the schedule sits here rather than in a block of its own.
  children?: React.ReactNode;
}) {
  const stored = draftOf(feature);
  const [draft, setDraft] = useState<FeatureDraft>(stored);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<string | null>(null);
  const [tested, setTested] = useState(false);

  // Where the position shown comes from: this level, or one above it. The
  // API resolves that; without it, an unset field is simply unset.
  const inherited = draft.enabled === null;
  const shown = draft.enabled ?? settled?.enabled ?? false;
  const above = settled?.inherited;
  const placeholder = (field: Placed): string =>
    inherits(
      above?.[field] ?? settled?.[field],
      above?.origins[field] ?? settled?.origins[field],
    );
  // Below the global level the switch is refused only when the feature is
  // disabled outright. A global default of off is not that: a project may
  // say otherwise, which is the whole point of it being a default.
  const gated = settled?.allowed === false && !root;
  const untested =
    withUrl &&
    probePath !== undefined &&
    draft.url.trim() !== "" &&
    draft.url.trim() !== stored.url &&
    !tested;

  /** Say nothing at this level at all, which is what clears the stored key.
   *
   * Not the same as saving an empty draft: that would store `allowed: true`
   * and leave a row saying so. A level that has nothing to say has no row.
   */
  async function clear() {
    setBusy(true);
    setError(null);
    try {
      await put(path, {});
      setDraft(EMPTY);
      setTested(false);
      onSaved();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function save(value: FeatureDraft) {
    setBusy(true);
    setError(null);
    try {
      await put(path, bodyOf(value));
      setDraft(value);
      setTested(false);
      onSaved();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    if (probePath === undefined) {
      return;
    }
    setBusy(true);
    setProbe(null);
    try {
      const answer = await post<{
        ok: boolean;
        server?: string;
        model?: string;
        dimensions?: number;
        detail?: string;
        fell_back?: boolean;
        skipped?: string[];
      }>(probePath, { url: draft.url.trim(), key: draft.key.trim() });
      setTested(answer.ok);
      setProbe(
        answer.ok
          ? `${answer.server} answered as ${answer.model}` +
              (answer.dimensions === undefined
                ? ""
                : `, ${answer.dimensions} dimensions`) +
              // The address that answered is not always the one typed: a
              // server that refuses this route is skipped, and saying so is
              // the difference between "it works" and "something else works".
              (answer.fell_back === true
                ? ` - NOT the address typed. ${(answer.skipped ?? []).join("; ")}`
                : "")
          : (answer.detail ?? "no server answered"),
      );
    } catch (reason: unknown) {
      setTested(false);
      setProbe(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {error !== null && <ErrorBox message={error} />}
      <div className="feature-row">
        {/* The kill switch, and then what a project that says nothing does.
            One field could not be both: turning a feature off by default
            would also have forbidden a project from turning itself on. */}
        {root && (
          <Switch
            checked={draft.allowed}
            label={draft.allowed ? "enabled" : "disabled"}
            title="whether this feature may run at all, for any project"
            onChange={(checked) => setDraft({ ...draft, allowed: checked })}
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
                ? `inherited from ${settled?.origins.enabled ?? "above"}: ${shown ? "on" : "off"}`
                : `status: ${shown ? "on" : "off"}`
          }
          title={
            gated
              ? "the feature is disabled, so this level is not asked at all"
              : root
                ? "what a project that says nothing about itself does"
                : undefined
          }
          onChange={(checked) => setDraft({ ...draft, enabled: checked })}
        />
        {children}
        {withUrl && (
          <input
            type="password"
            aria-label="Server token"
            autoComplete="new-password"
            className="token"
            placeholder="token, if the server wants one"
            value={draft.key}
            onChange={(event) => {
              setTested(false);
              setProbe(null);
              setDraft({ ...draft, key: event.target.value });
            }}
          />
        )}
        {withUrl && (
          <input
            type="text"
            aria-label="Server URL"
            placeholder={
              root && !above?.server_url
                ? "server URL, or the one in the environment"
                : placeholder("server_url")
            }
            value={draft.url}
            onChange={(event) => {
              setTested(false);
              setProbe(null);
              setDraft({ ...draft, url: event.target.value });
            }}
          />
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => void (untested ? test() : save(draft))}
        >
          {untested ? "Test" : "Save"}
        </button>
        {!inherited && (
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => void clear()}
            title={
              root
                ? "clear this level, back to the built-in default"
                : "clear this level, back to what the level above says"
            }
          >
            Inherit
          </button>
        )}
      </div>
      {/* The pace, under the row rather than in it: it is read far less
          often than the switch, and the row is already three controls wide.
          A project sets how big its own claims are; the poll interval and
          the work budget belong to the loop, so only the global page shows
          them. */}
      <div className="filters">
        <label>
          Files a claim
          <input
            type="number"
            min={1}
            max={64}
            placeholder={placeholder("batch")}
            value={draft.batch}
            onChange={(event) =>
              setDraft({ ...draft, batch: event.target.value })
            }
          />
        </label>
        {root && (
          <label>
            Poll when idle, seconds
            <input
              type="number"
              min={1}
              max={3600}
              placeholder={placeholder("tick_seconds")}
              value={draft.tick}
              onChange={(event) =>
                setDraft({ ...draft, tick: event.target.value })
              }
            />
          </label>
        )}
        <label>
          Chunk, characters
          <input
            type="number"
            min={200}
            max={20000}
            placeholder={placeholder("chunk_chars")}
            value={draft.chunkChars}
            onChange={(event) =>
              setDraft({ ...draft, chunkChars: event.target.value })
            }
          />
        </label>
        <label>
          Overlap, lines
          <input
            type="number"
            min={0}
            max={200}
            placeholder={placeholder("chunk_overlap")}
            value={draft.chunkOverlap}
            onChange={(event) =>
              setDraft({ ...draft, chunkOverlap: event.target.value })
            }
          />
        </label>
        {root && (
          <label>
            Work before re-reading these, seconds
            <input
              type="number"
              min={5}
              max={3600}
              placeholder={placeholder("budget_seconds")}
              value={draft.budget}
              onChange={(event) =>
                setDraft({ ...draft, budget: event.target.value })
              }
            />
          </label>
        )}
      </div>
      {withUrl && (
        <p className="muted">{urlInForce(settled, stored.url !== "")}</p>
      )}
      {probe !== null && <p className="muted">{probe}</p>}
      {withUrl && <KeyState feature={feature} />}
    </>
  );
}

/** What is known about the stored token, which is never the token itself.
 *
 * The date is a rotation reminder rather than a revocation: an expired key
 * goes on being sent, because a queue that stopped itself because a date
 * passed would be the silent failure these switches exist to prevent. Red is
 * what asks for a new one.
 */
function KeyState({ feature }: { feature: Feature | undefined }) {
  if (feature?.key_set !== true) {
    return null;
  }
  const due =
    feature.key_due === null || feature.key_due === undefined
      ? "no date recorded"
      : new Date(feature.key_due).toLocaleDateString("en-GB");
  return feature.key_expired === true ? (
    <p className="token-expired">token expired, please renew</p>
  ) : (
    <p className="token-set">token set, renew by {due}</p>
  );
}
