import { useCallback, useEffect, useState } from "react";

import { ApiError, get, post } from "../api.js";
import { Icon, ICONS } from "./Common.js";
import type { IndexJob } from "../types.js";

const POLL_MS = 2000;

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
}: {
  project: string;
  // One directory of the project, by the alias its node ids carry. Unset walks
  // every directory, which is the whole-project run.
  alias?: string;
  // What the buttons say they act on, for the hover text of the compact form.
  what?: string;
  compact?: boolean;
  onFinished: () => void;
}) {
  const [job, setJob] = useState<IndexJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const path = `/projects/${encodeURIComponent(project)}/index`;

  // Adopt a run already going, so a reload does not lose sight of it.
  useEffect(() => {
    let alive = true;
    get<IndexJob | null>(path)
      .then((found) => {
        if (alive && found !== null && found.status === "running") {
          setJob(found);
        }
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [path]);

  useEffect(() => {
    if (job === null || job.status !== "running") {
      return;
    }
    const timer = setInterval(() => {
      get<IndexJob | null>(path)
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
  }, [job, path, onFinished]);

  const start = useCallback(
    (fresh: boolean) => {
      setBusy(true);
      setError(null);
      post<IndexJob>(path, { fresh, alias: alias ?? "" })
        .then(setJob)
        .catch((failure: unknown) =>
          setError(
            failure instanceof ApiError ? failure.message : "could not start",
          ),
        )
        .finally(() => setBusy(false));
    },
    [path, alias],
  );

  const running = job !== null && job.status === "running";
  const subject = what ?? project;

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
        {error !== null && (
          <span className="bad" title={error}>
            !
          </span>
        )}
        {!running && job !== null && job.status === "failed" && (
          <span className="bad" title={job.error ?? ""}>
            !
          </span>
        )}
      </>
    );
  }

  return (
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
      {error !== null && <span className="bad">{error}</span>}
      {!running && job !== null && job.status === "failed" && (
        <span className="bad" title={job.error ?? ""}>
          failed
        </span>
      )}
      {!running && job !== null && job.status === "done" && (
        <span className="muted">{job.files ?? 0} files</span>
      )}
    </div>
  );
}
