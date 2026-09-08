import { useCallback, useEffect, useState } from "react";

import { ApiError, get, post } from "../api.js";
import { ErrorBox, Icon, ICONS } from "./Common.js";
import type { IndexJob } from "../types.js";

const POLL_MS = 2000;

/** Everything that went wrong in a run, as one block of text.
 *
 * A run of an organization is a fold over the runs of everything it holds, so
 * the failures are several and each one names its project. What was skipped
 * belongs here too: a member already indexing is why the run did less than it
 * was asked to.
 */
function failure(job: IndexJob): string {
  const lines = job.error === null ? [] : [job.error];
  for (const one of job.skipped ?? []) {
    lines.push(`${one.project}: skipped, ${one.why}`);
  }
  return lines.join("\n");
}

/**
 * Start an index run and follow it.
 *
 * The work happens in the API, which holds every tree at /code/<project>, so
 * this only has to ask and then watch: a run over a large codebase outlives
 * any request, and the row it writes is what says how it went.
 */
export function IndexButton({
  project,
  alias,
  what,
  compact = false,
  onFinished,
  onFailed,
}: {
  project: string;
  // One directory of the project, by the alias its node ids carry. Unset walks
  // every directory, which is the whole-project run.
  alias?: string;
  // What the buttons say they act on, for the hover text of the compact form.
  what?: string;
  compact?: boolean;
  onFinished: () => void;
  // Where the failure text goes when these buttons are a cell of a table: a
  // table cell is no place for a traceback, and an error nobody can read is
  // not reported at all. The compact form says nothing on its own.
  onFailed?: (message: string | null) => void;
}) {
  const [job, setJob] = useState<IndexJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const path = `/projects/${encodeURIComponent(project)}/index`;
  const query =
    alias === undefined || alias === ""
      ? ""
      : `?alias=${encodeURIComponent(alias)}`;

  // Adopt the last run, going or failed, so a run that nobody on this page
  // started - the schedule's, or another tab's - is not invisible.
  useEffect(() => {
    let alive = true;
    get<IndexJob | null>(`${path}${query}`)
      .then((found) => {
        if (
          alive &&
          found !== null &&
          (found.status === "running" || found.status === "failed")
        ) {
          setJob(found);
        }
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [path, query]);

  useEffect(() => {
    if (job === null || job.status !== "running") {
      return;
    }
    const timer = setInterval(() => {
      get<IndexJob | null>(`${path}${query}`)
        .then((found) => {
          if (found === null) {
            return;
          }
          setJob(found);
          if (found.status !== "running") {
            onFinished();
          }
        })
        .catch(() => undefined);
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [job, path, query, onFinished]);

  const start = useCallback(
    (fresh: boolean) => {
      setBusy(true);
      setError(null);
      post<IndexJob>(path, { fresh, alias: alias ?? "" })
        .then(setJob)
        .catch((failed: unknown) =>
          setError(
            failed instanceof ApiError ? failed.message : "could not start",
          ),
        )
        .finally(() => setBusy(false));
    },
    [path, alias],
  );

  const running = job !== null && job.status === "running";
  const subject = what ?? project;
  // Whatever refused to start, or whatever the last run recorded. It is text
  // under the control, never a tooltip and never behind a marker: an error is
  // read and pasted, not hunted for with a mouse.
  const wrong =
    error !== null
      ? error
      : !running && job !== null && job.status === "failed"
        ? failure(job)
        : null;

  // The compact form hands its failure to whoever placed it in a row, and that
  // is where the text appears. Reported on every render: the parent keeps the
  // message it already has, so an unchanged one costs nothing.
  useEffect(() => {
    onFailed?.(wrong);
  }, [wrong, onFailed]);

  if (compact) {
    return (
      <>
        <button
          type="button"
          disabled={busy || running}
          onClick={() => start(false)}
          title={
            running
              ? `Indexing ${subject}...`
              : `Index ${subject}, what changed`
          }
          aria-label={`Index ${subject}`}
        >
          <Icon path={running ? ICONS.running : ICONS.index} />
        </button>
        <button
          type="button"
          disabled={busy || running}
          onClick={() => start(true)}
          title={`Index ${subject} again from scratch, trusting no cache`}
          aria-label={`Index ${subject} from scratch`}
        >
          <Icon path={ICONS.fresh} />
        </button>
      </>
    );
  }

  return (
    <div className="index-block">
      <div className="index-control">
        <button
          type="button"
          disabled={busy || running}
          onClick={() => start(false)}
          title="Walk the tree and refresh what changed"
        >
          {running ? "Indexing..." : "Index"}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy || running}
          onClick={() => start(true)}
          title="Trust neither cache and parse every file again"
        >
          Fresh
        </button>
        {!running && job !== null && job.status === "done" && (
          <span className="muted">{job.files ?? 0} files</span>
        )}
      </div>
      {wrong !== null && (
        <ErrorBox message={wrong} what={`indexing ${subject} failed`} />
      )}
    </div>
  );
}
