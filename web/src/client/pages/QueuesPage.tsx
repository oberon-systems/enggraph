import { useState } from "react";
import { Link } from "react-router";

import {
  Count,
  Coverage,
  coverageTone,
  Empty,
  ErrorBox,
  percentOf,
  Spinner,
  waitingOf,
} from "../components/Common.js";
import { NotProcessed, RetryIcon } from "../components/NotProcessed.js";
import { Switch } from "../components/Switch.js";
import { useApi } from "../hooks/useApi.js";
import type { EmbeddingsView, SummariesView } from "../types.js";

// How often the page asks again while the switch is on. Faster than either
// queue ticks, so a file finishing shows up within a refresh of itself.
const REFRESH_MS = 5_000;

// What a file described by a person is: finished, and not the model's to do.
// It is taken out of the total rather than counted as done - a percent that
// can never reach 100 is indistinguishable from a broken one.
function owed(summary: { files: number; manual: number }): number {
  return Math.max(0, summary.files - summary.manual);
}

/** What the two model-driven queues are doing, and how far each project is.
 *
 * The one page that answers "is it done yet". Both queues are the same shape
 * - files owed, files carried, a depth and a percent - so they are rendered
 * by the same two components rather than by two tables that drift apart.
 */
export function QueuesPage() {
  const [live, setLive] = useState(true);
  const every = live ? REFRESH_MS : 0;
  const summaries = useApi<SummariesView>("/summaries", every);
  const embeddings = useApi<EmbeddingsView>("/embeddings", every);

  function reload() {
    summaries.reload();
    embeddings.reload();
  }

  const error = summaries.error ?? embeddings.error;
  if (error !== null) {
    return <ErrorBox message={error} />;
  }
  const summaryRows = summaries.data;
  const embeddingRows = embeddings.data;
  if (summaryRows === null || embeddingRows === null) {
    return <Spinner what="the queues" />;
  }

  const rows = summaryRows.summaries.map((summary) => ({
    project: summary.project,
    summary,
    embedding: embeddingRows.embeddings.find(
      (one) => one.project === summary.project,
    ),
  }));

  return (
    <>
      <div className="row">
        <h1>Queues</h1>
        <Switch
          checked={live}
          label={live ? "refreshing every 5s" : "refresh paused"}
          title="ask again on a timer, or only when this page is opened"
          onChange={setLive}
        />
        <button
          type="button"
          className="secondary"
          onClick={() => {
            summaries.reload();
            embeddings.reload();
          }}
        >
          Refresh
        </button>
        <RetryIcon queues={["summaries", "embeddings"]} onRetried={reload} />
      </div>
      <p className="muted">
        What the model is working through, and what is left. Both queues fill
        themselves when a project is switched on and drain in the background;
        neither blocks indexing or search.
      </p>

      <div className="tiles">
        <Totals
          title="Summarizing"
          failed={sum(rows.map((row) => row.summary.skipped ?? 0))}
          // Manual summaries count as covered: a file described by a person
          // through save_node_summary is finished, and the model is forbidden
          // from touching it. Counting only its own work would leave every
          // such file looking owed for ever.
          done={sum(rows.map((row) => row.summary.described))}
          total={sum(rows.map((row) => owed(row.summary)))}
          queue={merge(
            rows
              .filter((row) => row.summary.enabled)
              .map((row) => row.summary.queue),
          )}
          note={
            summaryRows.loop
              ? `${rows.filter((row) => row.summary.pushed).length} project(s) pushed at a server`
              : "the push loop is off in this process"
          }
        />
        <Totals
          title="Embedding"
          failed={sum(rows.map((row) => row.embedding?.skipped ?? 0))}
          done={sum(rows.map((row) => row.embedding?.files ?? 0))}
          total={sum(rows.map((row) => row.embedding?.indexed_files ?? 0))}
          queue={merge(
            rows
              .filter((row) => row.embedding?.enabled ?? false)
              .map((row) => row.embedding?.queue ?? {}),
          )}
          note={
            `${sum(rows.map((row) => row.embedding?.chunks ?? 0)).toLocaleString("en-US")} ` +
            `chunk(s) of ~${embeddingRows.chunk_chars} characters, as ` +
            embeddingRows.model
          }
        />
      </div>

      {rows.length === 0 ? (
        <Empty>
          No project is mounted, so neither queue has anything to do.
        </Empty>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Project</th>
              <th title="files a model has described, of the files there are">
                Summarised
              </th>
              <th title="files still owed a summary">Queued</th>
              <th title="files given up on, or not done because it is off">
                Skip
              </th>
              <th title="files with vectors, of the files indexed">Embedded</th>
              <th title="files still owed vectors">Queued</th>
              <th title="files given up on, or not done because it is off">
                Skip
              </th>
              <th title="chunks written; a file is many of them">Chunks</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.project}>
                <td>
                  <Link
                    to={`/projects/${encodeURIComponent(row.project)}?tab=settings`}
                  >
                    {row.project}
                  </Link>
                </td>
                <Progress
                  done={row.summary.described}
                  total={owed(row.summary)}
                  enabled={row.summary.enabled}
                  gated={row.summary.gated}
                  waiting={waitingOf(row.summary.queue)}
                  failed={row.summary.skipped ?? 0}
                  note={
                    row.summary.manual === 0
                      ? undefined
                      : `${row.summary.manual} file(s) written by hand are left out: ` +
                        "the model never overwrites those"
                  }
                />
                <Queued
                  enabled={row.summary.enabled}
                  queue={row.summary.queue}
                />
                <Skipped
                  project={row.project}
                  name="summaries"
                  enabled={row.summary.enabled}
                  queue={row.summary.queue}
                  skipped={row.summary.skipped ?? 0}
                  onRetried={reload}
                />
                <Progress
                  done={row.embedding?.files ?? 0}
                  total={row.embedding?.indexed_files ?? 0}
                  enabled={row.embedding?.enabled ?? false}
                  gated={row.embedding?.gated ?? false}
                  waiting={waitingOf(row.embedding?.queue ?? {})}
                  failed={row.embedding?.skipped ?? 0}
                />
                <Queued
                  enabled={row.embedding?.enabled ?? false}
                  queue={row.embedding?.queue ?? {}}
                />
                <Skipped
                  project={row.project}
                  name="embeddings"
                  enabled={row.embedding?.enabled ?? false}
                  queue={row.embedding?.queue ?? {}}
                  skipped={row.embedding?.skipped ?? 0}
                  onRetried={reload}
                />
                <td
                  className="num muted"
                  title="chunks written for this project"
                >
                  {(row.embedding?.chunks ?? 0).toLocaleString("en-US")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

/** Add up the same queue across every project, state by state. */
function merge(queues: Record<string, number>[]): Record<string, number> {
  const total: Record<string, number> = {};
  for (const queue of queues) {
    for (const [state, count] of Object.entries(queue)) {
      total[state] = (total[state] ?? 0) + count;
    }
  }
  return total;
}

/** One queue at a glance: how far it has got, and how much is waiting. */
function Totals({
  title,
  done,
  total,
  queue,
  failed,
  note,
}: {
  title: string;
  done: number;
  total: number;
  queue: Record<string, number>;
  failed: number;
  note: string;
}) {
  const waiting = waitingOf(queue);
  const percent = percentOf(done, total);
  const tone = coverageTone(done, total, waiting, failed) ?? "";
  return (
    <div className="tile">
      <div className="tile-label">{title}</div>
      <div className={`tile-value ${tone}`}>
        {percent === null ? "-" : `${percent}%`}
      </div>
      <div className="muted">
        <Count value={done} /> of <Count value={total} /> file(s)
        {waiting === 0 ? (
          ", nothing queued"
        ) : (
          <>
            , <Count value={waiting} /> queued
          </>
        )}
        {failed > 0 ? (
          <span className="token-expired">
            , <Count value={failed} /> not processed
          </span>
        ) : null}
      </div>
      <div className="muted">{note}</div>
    </div>
  );
}

/** A project's share of one queue, or why it has none. */
function Progress({
  done,
  total,
  enabled,
  gated,
  note,
  waiting,
  failed,
}: {
  done: number;
  total: number;
  enabled: boolean;
  gated: boolean;
  note?: string;
  waiting: number;
  failed: number;
}) {
  return (
    <td>
      <Coverage
        done={done}
        total={total}
        muted={!enabled}
        title={note}
        waiting={waiting}
        failed={failed}
      />
      {!enabled && <div className="muted">{gated ? "disabled" : "off"}</div>}
    </td>
  );
}

/** Files still owed work, only where the queue is switched on. */
function Queued({
  enabled,
  queue,
}: {
  enabled: boolean;
  queue: Record<string, number>;
}) {
  const waiting = waitingOf(queue);
  return enabled && waiting > 0 ? (
    <td className="num">{waiting}</td>
  ) : (
    <td className="muted">-</td>
  );
}

/** Files not to be processed: given up on, or left in a queue that is off. */
function Skipped({
  project,
  name,
  enabled,
  queue,
  skipped,
  onRetried,
}: {
  project: string;
  name: "summaries" | "embeddings";
  enabled: boolean;
  queue: Record<string, number>;
  skipped: number;
  onRetried: () => void;
}) {
  const off = enabled ? 0 : waitingOf(queue);
  if (skipped === 0 && off === 0) {
    return <td className="muted">-</td>;
  }
  return (
    <td className="num">
      {off > 0 && (
        <span
          className="muted"
          title="switched off for this project: left in an old job, not processed"
        >
          {off} (off)
        </span>
      )}
      <NotProcessed
        project={project}
        queue={name}
        count={skipped}
        onRetried={onRetried}
      />
    </td>
  );
}
