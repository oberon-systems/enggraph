import { badRequest, readBodyNumber, readBodyString } from "./args.js";

// The switchable features, as enggraph.config names them. `indexing` is the
// key the schedule already lives under: the switch is another field of that
// object rather than a second place to look.
export const FEATURES = ["indexing", "summarize", "embedding"] as const;
export type FeatureName = (typeof FEATURES)[number];

// A null field is this level letting go of it: stored as a removal, so the
// level above answers. An absent field leaves whatever is stored alone.
export type Feature = {
  // Whether the feature may run at all. Only the global level stores it: a
  // project cannot allow itself something the operator switched off.
  allowed?: boolean;
  enabled?: boolean | null;
  server_url?: string | null;
  server_key?: string;
  key_saved_at?: string;
  // How fast the queue behind this feature is worked. `batch` and the chunk
  // pair are per project; the other two belong to the loop and are global.
  batch?: number | null;
  tick_seconds?: number | null;
  budget_seconds?: number | null;
  chunk_chars?: number | null;
  chunk_overlap?: number | null;
};

// The same bounds enggraph.features clamps to when it reads a row. A value
// refused here can still arrive through psql, so neither side is the only
// guard.
const NUMBERS: Record<string, [number, number]> = {
  batch: [1, 64],
  tick_seconds: [1, 3600],
  budget_seconds: [5, 3600],
  chunk_chars: [200, 20000],
  chunk_overlap: [0, 200],
};
// Only the loop reads these, and the loop reads the global level.
const ROOT_ONLY = new Set(["tick_seconds", "budget_seconds"]);

/** Name a feature, or refuse: the path segment reaches the database as a key. */
export function requireFeature(value: string): FeatureName {
  if (!FEATURES.includes(value as FeatureName)) {
    throw badRequest(
      `Unknown feature "${value}", one of ${FEATURES.join(", ")}`,
    );
  }
  return value as FeatureName;
}

/**
 * Check a server URL before it is stored.
 *
 * Refused here as well as in Python, for the reason the schedule's bounds are
 * checked in both: a row can be written in psql, so neither side is the only
 * guard - but a URL typed in the dashboard should be refused where it was
 * typed rather than by a queue that quietly stops an hour later.
 */
function readServerUrl(body: unknown): string | undefined {
  if ((body as Record<string, unknown>).server_url === null) {
    return "";
  }
  const value = readBodyString(body, "server_url");
  if (value === undefined) {
    return undefined;
  }
  const url = value.trim().replace(/\/$/, "");
  if (url === "") {
    return "";
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw badRequest('Field "server_url" must be an http or https URL');
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw badRequest('Field "server_url" must be an http or https URL');
  }
  return url;
}

/**
 * Read a feature out of a request body, or null when it states nothing.
 *
 * A body that leaves every field out is a level with nothing to say, which is
 * stored by removing the key rather than by writing an empty object - the
 * same rule the schedule follows, and what sends the question back to the
 * level above.
 */
function readSwitch(body: unknown, name: string): boolean | null | undefined {
  const value = (body as Record<string, unknown>)[name];
  if (value === undefined || value === null) {
    return value;
  }
  if (typeof value !== "boolean") {
    throw badRequest(`Field "${name}" must be true or false`);
  }
  return value;
}

function present(body: unknown, name: string): boolean {
  return (body as Record<string, unknown>)[name] !== undefined;
}

export function readFeature(body: unknown, root = false): Feature | null {
  if (body === null || typeof body !== "object") {
    throw badRequest("A JSON object body is required");
  }
  const value: Feature = {};
  // `allowed` is dropped rather than refused below the global level: the
  // dashboard sends one shape, and a project has nothing to say about it.
  const allowed = readSwitch(body, "allowed");
  if (root && typeof allowed === "boolean") {
    value.allowed = allowed;
  }
  const enabled = readSwitch(body, "enabled");
  if (enabled !== undefined) {
    value.enabled = enabled;
  }
  const url = readServerUrl(body);
  if (url !== undefined) {
    value.server_url = url === "" ? null : url;
  }
  for (const [name, [low, high]] of Object.entries(NUMBERS)) {
    // The loop's own pace is only meaningful where the loop reads it, which
    // is the global level; a project sets how big its own claims are.
    if ((!root && ROOT_ONLY.has(name)) || !present(body, name)) {
      continue;
    }
    (value as Record<string, unknown>)[name] =
      readBodyNumber(body, name, low, high) ?? null;
  }
  // Write-only. An absent or empty field leaves whatever is stored alone,
  // which is what makes the input in the dashboard a way to replace a token
  // rather than a way to wipe one by saving the form it is not typed into.
  const key = readBodyString(body, "server_key")?.trim();
  if (key !== undefined && key !== "") {
    value.server_key = key;
    value.key_saved_at = new Date().toISOString();
  }
  return Object.keys(value).length === 0 ? null : value;
}

// How long a stored token is good for before the dashboard asks for a new
// one. The other copy of this number is enggraph.config.FEATURE_KEY_TTL_DAYS,
// which is what the Python side reports; they are read side by side on one
// page, so they have to agree.
const KEY_TTL_DAYS = 30;

type Redacted = Record<string, unknown>;

/**
 * Take the tokens out of a settings object before it is sent to a browser.
 *
 * The dashboard has no authentication of its own: a secret that travelled
 * back to it would be readable by anyone who can open the page, and by
 * anything that caches the response. What a reader needs is whether a token
 * is stored and whether it is due for rotation, and neither is the secret.
 */
export function redactKeys(settings: unknown): Redacted | null {
  if (settings === null || typeof settings !== "object") {
    return null;
  }
  const out: Redacted = {};
  for (const [key, value] of Object.entries(settings as Redacted)) {
    if (value === null || typeof value !== "object") {
      out[key] = value;
      continue;
    }
    const feature = { ...(value as Redacted) };
    const stored = feature.server_key;
    const savedAt = feature.key_saved_at;
    delete feature.server_key;
    if (typeof stored === "string" && stored !== "") {
      const saved = typeof savedAt === "string" ? Date.parse(savedAt) : NaN;
      const due = Number.isNaN(saved)
        ? null
        : new Date(saved + KEY_TTL_DAYS * 86_400_000);
      feature.key_set = true;
      feature.key_due = due === null ? null : due.toISOString();
      feature.key_expired = due !== null && due.getTime() < Date.now();
    }
    out[key] = feature;
  }
  return out;
}
