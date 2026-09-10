import { useState } from "react";

import { post, put, remove } from "../api.js";
import {
  Count,
  embeddedPercent,
  Empty,
  ErrorBox,
  SelectionBadge,
  Spinner,
  minutes,
} from "../components/Common.js";
import { FeatureEditor } from "../components/FeatureFields.js";
import { IndexingEditor } from "../components/IndexingFields.js";
import { useApi } from "../hooks/useApi.js";
import type {
  EmbeddingsView,
  FileType,
  Page,
  ProjectFeatures,
  ProjectSchedule,
  ProjectSettings,
  ScanResult,
  SettingsLevel,
} from "../types.js";

/** What a project indexes, and where that answer comes from.
 *
 * The two documents are the same commented text a `.enggraph-keep` and a
 * `.enggraph-ignore` hold, because they are that: a repository that still ships the
 * pair keeps deciding its own index, and what is edited here takes over only
 * once those files are gone.
 */
export function SettingsTab({
  project,
  organization,
}: {
  project: string;
  organization: boolean;
}) {
  const settings = useApi<ProjectSettings>(
    `/projects/${encodeURIComponent(project)}/settings`,
  );
  const types = useApi<Omit<Page<FileType>, "total" | "limit" | "offset">>(
    `/projects/${encodeURIComponent(project)}/file-types`,
  );
  const schedule = useApi<ProjectSchedule>(
    `/projects/${encodeURIComponent(project)}/schedule`,
  );
  const featured = useApi<ProjectFeatures>(
    `/projects/${encodeURIComponent(project)}/features`,
  );
  // Every project's embedding state in one call, as the API answers it; the
  // row for this one is what the section below reports.
  const embeddings = useApi<EmbeddingsView>("/embeddings");
  const path = `/projects/${encodeURIComponent(project)}`;

  if (settings.error !== null) {
    return <ErrorBox message={settings.error} />;
  }
  if (settings.data === null) {
    return <Spinner what="the settings" />;
  }
  // An organization reads no tree, so there is nothing for a scan to propose
  // from and no file in a tree that could be deciding this.
  const own = settings.data.project;

  return (
    <>
      <h2>File types indexed now</h2>
      {types.data === null || types.data.items.length === 0 ? (
        <Empty>
          Nothing is in the graph yet, so there is no answer to what this
          project indexes. Index it and this fills in.
        </Empty>
      ) : (
        <div className="chips">
          {types.data.items.map((entry) => (
            <span key={entry.extension} className="chip static">
              {entry.extension} <Count value={entry.count} />
            </span>
          ))}
        </div>
      )}
      <p className="muted">
        What the last index run actually wrote, not what the selection below
        would pick up. The two differ whenever the selection has changed since.
      </p>

      <h2>Indexing</h2>
      {schedule.data !== null && <Effective schedule={schedule.data} />}
      <IndexingEditor
        key={`project-${own?.updated_at ?? "none"}`}
        path={`${path}/indexing`}
        indexing={own?.settings?.indexing}
        feature={own?.settings?.indexing}
        settled={featured.data?.features.indexing}
        schedule={schedule.data ?? undefined}
        onSaved={() => {
          settings.reload();
          featured.reload();
          schedule.reload();
        }}
      />

      <h2>Summarizing</h2>
      <p className="muted">
        Whether a file of this project may be described by a model, and the
        server that answers. Empty inherits the address from the level above.
      </p>
      <FeatureEditor
        key={`summarize-${own?.updated_at ?? "none"}`}
        path={`${path}/features/summarize`}
        probePath="/summaries/probe"
        feature={own?.settings?.summarize}
        settled={featured.data?.features.summarize}
        onSaved={() => {
          settings.reload();
          featured.reload();
        }}
      />

      <h2>Embedding</h2>
      <EmbeddingProgress project={project} view={embeddings.data} />
      <FeatureEditor
        key={`embedding-${own?.updated_at ?? "none"}`}
        path={`${path}/features/embedding`}
        probePath="/embeddings/probe"
        feature={own?.settings?.embedding}
        settled={featured.data?.features.embedding}
        onSaved={() => {
          settings.reload();
          featured.reload();
          embeddings.reload();
        }}
      />

      <h2>Selection</h2>
      {/* One level, this project's own. A member of an organization falls
          back to what that organization sets, on its own page. */}
      <Level
        project={project}
        scannable={!organization}
        heading={
          organization ? (
            "This organization, for every project it holds"
          ) : (
            <>
              The whole tree{" "}
              <span className="path">{own?.root_path ?? ""}</span>
            </>
          )
        }
        level={own}
        origins={own === null ? [] : [own.keep_source, own.ignore_source]}
        onSaved={() => {
          settings.reload();
        }}
      />

      <p className="muted">
        This level and no other. A project belonging to an organization falls
        back to what that organization sets, and everything falls back to the
        global default in the end. A <code>.enggraph-keep</code> or{" "}
        <code>.enggraph-ignore</code> still in the tree beats every one of them,
        and goes on doing so until it is deleted.
      </p>
    </>
  );
}

/** How much of a project has vectors, and how much is still waiting.
 *
 * The two numbers are read against the file count on purpose: forty files
 * embedded out of forty is finished, out of four thousand it has barely
 * started, and the pair looks the same without the third.
 */
function EmbeddingProgress({
  project,
  view,
}: {
  project: string;
  view: EmbeddingsView | null;
}) {
  const row = view?.embeddings.find((one) => one.project === project);
  if (view === null || row === undefined) {
    return null;
  }
  const waiting = (row.queue.pending ?? 0) + (row.queue.running ?? 0);
  const percent = embeddedPercent(row);
  return (
    <p className="muted">
      {percent === null ? "No files" : `${percent}%`}:{" "}
      <Count value={row.files} /> of <Count value={row.indexed_files} /> files
      embedded in <Count value={row.chunks} /> chunks, as {view.model}.{" "}
      {waiting === 0 ? (
        row.enabled ? (
          "Nothing is queued."
        ) : (
          "Nothing is queued, and embedding is off for this project."
        )
      ) : (
        <>
          <Count value={waiting} /> file(s) queued
          {row.queue.failed ? (
            <>
              , <Count value={row.queue.failed} /> failed
            </>
          ) : null}
          .
        </>
      )}
    </p>
  );
}

/** What the levels above come to, resolved into the one schedule they make.
 *
 * The resolution is the API's. Stating it here is what keeps the rule from
 * becoming folklore about a settings page.
 */
function Effective({ schedule }: { schedule: ProjectSchedule }) {
  const when = (stamp: string | null) =>
    stamp === null ? "never" : new Date(stamp).toLocaleString("en-GB");
  return (
    <>
      <p className={schedule.mode === "off" ? "muted" : undefined}>
        {schedule.mode === "off" ? (
          <>
            Nothing indexes this project on its own. The Index button on the
            overview tab is the only thing that starts a run.
          </>
        ) : schedule.mode === "periodic" ? (
          <>Indexed every {minutes(schedule.interval_minutes)}.</>
        ) : (
          <>
            Watching the tree: indexed when it changes, at most once every{" "}
            {minutes(schedule.debounce_minutes)}, and swept every{" "}
            {minutes(schedule.interval_minutes)} regardless.
          </>
        )}{" "}
        <span className="muted">
          Last run {when(schedule.last_run)}
          {schedule.next_run !== null && (
            <> - next sweep {when(schedule.next_run)}</>
          )}
          .
        </span>
      </p>
      {!schedule.scheduler && schedule.mode !== "off" && (
        <p className="stale">
          The service is running with INDEX_SCHEDULER off, so nothing acts on
          this. What is saved here takes effect when it is turned back on.
        </p>
      )}
    </>
  );
}

/** One editable level of the selection.
 *
 * `origins` is empty for a level with no tree behind it, which is what an
 * organization is: nothing there read a selection, so nothing is reported.
 */
function Level({
  project,
  scannable,
  heading,
  level,
  origins,
  onSaved,
}: {
  project: string;
  // Whether there is a tree for a scan to propose a selection from.
  scannable: boolean;
  heading: React.ReactNode;
  level: SettingsLevel | null;
  origins: (string | null)[];
  onSaved: () => void;
}) {
  const [keep, setKeep] = useState(level?.ctxkeep ?? "");
  const [ignore, setIgnore] = useState(level?.ctxignore ?? "");
  const [report, setReport] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const path = `/projects/${encodeURIComponent(project)}`;
  const shadowed = origins.includes("file");

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="level">
      <div className="row">
        <h3>{heading}</h3>
        {origins.length > 0 && (
          <span>
            ctxkeep <SelectionBadge origin={origins[0] ?? null} /> ctxignore{" "}
            <SelectionBadge origin={origins[1] ?? null} />
          </span>
        )}
      </div>

      {error !== null && <ErrorBox message={error} />}
      {shadowed && (
        <p className="stale">
          A selection file in the tree is deciding this project. What is saved
          here is stored and unused until that file is deleted.
        </p>
      )}

      <div className="editors">
        <label>
          ctxkeep - what becomes a node. Empty falls back to the level above.
          <textarea
            value={keep}
            rows={16}
            onChange={(event) => setKeep(event.target.value)}
          />
        </label>
        <label>
          ctxignore - what is pruned, on top of the built-in skip list.
          <textarea
            value={ignore}
            rows={16}
            onChange={(event) => setIgnore(event.target.value)}
          />
        </label>
      </div>

      {report !== null && <pre className="report">{report}</pre>}

      <div className="row">
        <button
          type="button"
          disabled={busy || !scannable}
          title={
            scannable
              ? "propose a selection from the file types this tree holds"
              : "a scan reads a tree, and this level has none of its own"
          }
          onClick={() =>
            void run(async () => {
              const proposed = await post<ScanResult>(`${path}/scan`, {});
              setKeep(proposed.ctxkeep);
              setIgnore(proposed.ctxignore);
              setReport(proposed.report);
            })
          }
        >
          Regenerate from the tree
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              await remove(`${path}/settings`);
              setKeep("");
              setIgnore("");
              setReport(null);
              onSaved();
            })
          }
        >
          Reset to the level above
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              await put(`${path}/settings`, {
                ctxkeep: keep,
                ctxignore: ignore,
              });
              onSaved();
            })
          }
        >
          Save
        </button>
      </div>
    </div>
  );
}
