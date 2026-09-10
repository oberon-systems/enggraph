import { useState } from "react";
import { Link } from "react-router";

import { post } from "../api.js";
import { useApi } from "../hooks/useApi.js";
import type { Failures } from "../types.js";
import { Empty, ErrorBox, Icon, ICONS, Spinner } from "./Common.js";

type Queue = "summaries" | "embeddings";

/** A retry as an icon: one project's failed files, or every project's. */
export function RetryIcon({
  queues,
  project,
  onRetried,
}: {
  queues: Queue[];
  project?: string;
  onRetried: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const what = project === undefined ? "every project" : `project ${project}`;

  async function retry() {
    setBusy(true);
    try {
      for (const queue of queues) {
        await post(`/${queue}/retry`, { project: project ?? "" });
      }
      onRetried();
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      className="secondary icon-button"
      disabled={busy}
      title={`Put the failed files of ${what} back in the queue`}
      aria-label={`Retry the failed files of ${what}`}
      onClick={() => void retry()}
    >
      <Icon path={ICONS.retry} />
    </button>
  );
}

/** A red count of what a queue gave up on, linking to the project's list. */
export function NotProcessed({
  project,
  queue,
  count,
  onRetried,
}: {
  project: string;
  queue: Queue;
  count: number;
  onRetried: () => void;
}) {
  if (count <= 0) {
    return null;
  }
  return (
    <span className="not-processed">
      <Link
        className="failed-count"
        to={`/projects/${encodeURIComponent(project)}?tab=failures`}
        target="_blank"
        rel="noreferrer"
        title={`${count} file(s) not processed: open the list`}
      >
        {count}
      </Link>
      <RetryIcon queues={[queue]} project={project} onRetried={onRetried} />
    </span>
  );
}

/** The project's own tab: every file each queue gave up on, and why. */
export function FailuresTab({ project }: { project: string }) {
  const failures = useApi<Failures>(
    `/projects/${encodeURIComponent(project)}/failures`,
  );
  if (failures.error !== null) {
    return <ErrorBox message={failures.error} />;
  }
  if (failures.data === null) {
    return <Spinner what="the failures" />;
  }
  const sections: [Queue, string, { file_path: string; error: string }[]][] = [
    ["summaries", "Summarizing", failures.data.summaries],
    ["embeddings", "Embedding", failures.data.embeddings],
  ];
  return (
    <>
      {sections.map(([queue, title, files]) => (
        <section key={queue}>
          <div className="row">
            <h2>
              {title}: {files.length} not processed
            </h2>
            {files.length > 0 && (
              <RetryIcon
                queues={[queue]}
                project={project}
                onRetried={failures.reload}
              />
            )}
          </div>
          {files.length === 0 ? (
            <Empty>Nothing failed.</Empty>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {files.map((one) => (
                  <tr key={one.file_path}>
                    <td>
                      <code>{one.file_path}</code>
                    </td>
                    <td className="muted">{one.error}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </>
  );
}
