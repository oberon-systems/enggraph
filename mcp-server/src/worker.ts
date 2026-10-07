// The worker API holds every tree, mounted read-only; the MCP server holds
// none and keeps no file text, so whatever shows a file's text asks it here.
export const WORKER_API_URL = (process.env.WORKER_API_URL ?? "").replace(
  /\/$/,
  "",
);
export const WORKER_API_TOKEN = process.env.WORKER_API_TOKEN ?? "";

const RANGES_TIMEOUT_MS = 10_000;
const GREP_TIMEOUT_MS = 90_000;
const DROP_TIMEOUT_MS = 600_000;
const MAX_RANGES = 200;

export interface TextRange {
  project: string;
  path: string;
  start: number;
  end: number;
}

export interface GrepRequest {
  projects: string[];
  pattern: string;
  regex: boolean;
  loose: boolean;
  path: string;
  limit: number;
}

export interface GrepMatch {
  project: string;
  path: string;
  line: number;
  text: string;
}

export function workerConfigured(): boolean {
  return WORKER_API_URL !== "" && WORKER_API_TOKEN !== "";
}

async function post<T>(
  path: string,
  body: unknown,
  timeout: number,
): Promise<T> {
  const answer = await fetch(`${WORKER_API_URL}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${WORKER_API_TOKEN}`,
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeout),
  });
  if (!answer.ok) {
    const detail = await answer.text();
    throw new Error(`worker API ${path}: ${answer.status} ${detail}`);
  }
  return (await answer.json()) as T;
}

/**
 * The text of each range, read from the mounts, null where it cannot be.
 *
 * Never throws: a packet or a search without its snippets is still an answer.
 */
export async function readRanges(
  ranges: TextRange[],
): Promise<(string | null)[]> {
  if (ranges.length === 0 || !workerConfigured()) {
    return ranges.map(() => null);
  }
  const texts: (string | null)[] = [];
  try {
    for (let at = 0; at < ranges.length; at += MAX_RANGES) {
      const batch = ranges.slice(at, at + MAX_RANGES);
      const body = await post<{ ranges: { text: string | null }[] }>(
        "/content/ranges",
        { ranges: batch },
        RANGES_TIMEOUT_MS,
      );
      texts.push(...batch.map((_, index) => body.ranges[index]?.text ?? null));
    }
  } catch {
    return ranges.map(() => null);
  }
  return texts;
}

/** Lines of the mounted trees matching a pattern; throws when it cannot ask. */
export async function grepTrees(
  ask: GrepRequest,
): Promise<{ matches: GrepMatch[]; truncated: boolean }> {
  if (!workerConfigured()) {
    throw new Error(
      "No worker API is configured: the trees are read through it, so a " +
        "search over their text cannot run",
    );
  }
  return post("/grep", ask, GREP_TIMEOUT_MS);
}

/** Delete a project and every row naming it; throws when it cannot ask. */
export async function dropProject(project: string): Promise<void> {
  if (!workerConfigured()) {
    throw new Error(
      "No worker API is configured: a project is dropped through it, " +
        "because it is what names every table the project has rows in",
    );
  }
  await post<unknown>(
    `/projects/${encodeURIComponent(project)}/drop`,
    {},
    DROP_TIMEOUT_MS,
  );
}
