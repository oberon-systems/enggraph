import { badRequest, readBodyEnum, readBodyNumber } from "./args.js";

// The key `project_settings.settings` holds a schedule under, and the modes
// it may name, as enggraph.config spells both. The bounds are the same ones
// enggraph.schedule clamps to when it reads a row: a value refused here can
// still arrive through psql, so neither side is the only guard.
export const INDEXING_KEY = "indexing";
export const INDEXING_MODES = ["off", "periodic", "auto"] as const;
const MIN_INTERVAL = 1;
const MAX_INTERVAL = 10080;
const MIN_DEBOUNCE = 1;
const MAX_DEBOUNCE = 1440;

export type Indexing = {
  // The kill switch, stored only at the global level, in the same object as
  // the schedule it gates.
  allowed?: boolean;
  mode?: string;
  interval_minutes?: number;
  debounce_minutes?: number;
  // The switch, stored in the same object as the schedule it gates and
  // written by the same request: this key is replaced rather than merged, so
  // a second request for the switch alone would drop the schedule.
  enabled?: boolean;
};

/**
 * Read a schedule out of a request body, or null when it states nothing.
 *
 * A field left out inherits from the level above, so a body that leaves out
 * all of them is a level with nothing to say - which is stored by removing
 * the key rather than by writing an empty object.
 */
export function readIndexing(body: unknown, root = false): Indexing | null {
  const value: Indexing = {};
  const allowed = (body as Record<string, unknown> | undefined)?.allowed;
  if (root && allowed !== undefined && allowed !== null) {
    if (typeof allowed !== "boolean") {
      throw badRequest('Field "allowed" must be true or false');
    }
    value.allowed = allowed;
  }
  const mode = readBodyEnum(body, "mode", INDEXING_MODES);
  if (mode !== undefined) {
    value.mode = mode;
  }
  const interval = readBodyNumber(
    body,
    "interval_minutes",
    MIN_INTERVAL,
    MAX_INTERVAL,
  );
  if (interval !== undefined) {
    value.interval_minutes = interval;
  }
  const debounce = readBodyNumber(
    body,
    "debounce_minutes",
    MIN_DEBOUNCE,
    MAX_DEBOUNCE,
  );
  if (debounce !== undefined) {
    value.debounce_minutes = debounce;
  }
  const enabled = (body as Record<string, unknown> | undefined)?.enabled;
  if (enabled !== undefined && enabled !== null) {
    if (typeof enabled !== "boolean") {
      throw badRequest('Field "enabled" must be true or false');
    }
    value.enabled = enabled;
  }
  return Object.keys(value).length === 0 ? null : value;
}
