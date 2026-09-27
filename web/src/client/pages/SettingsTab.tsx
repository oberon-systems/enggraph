import { useState } from "react";

import { post, put } from "../api.js";
import {
  Count,
  embeddedPercent,
  Empty,
  ErrorBox,
  Spinner,
  minutes,
} from "../components/Common.js";
import { FeatureEditor, UnsavedNote } from "../components/FeatureFields.js";
import { IndexingEditor } from "../components/IndexingFields.js";
import { NotProcessed } from "../components/NotProcessed.js";
import { LAMP_REFRESH_MS, ServerLines } from "../components/StatusLamps.js";
import { useApi } from "../hooks/useApi.js";
import { unsaved, useDraft } from "../hooks/useDraft.js";
import type {
  EmbeddingsView,
  FileType,
  Page,
  ProjectFeatures,
  ProjectSchedule,
  ProjectSettings,
  InheritedIgnore,
  SettingsLevel,
  SummariesView,
} from "../types.js";

/** What a project indexes: the formats its runs found, and what it prunes. */
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
  const embeddings = useApi<EmbeddingsView>("/embeddings", LAMP_REFRESH_MS);
  const summaries = useApi<SummariesView>("/summaries", LAMP_REFRESH_MS);
  const summaryRow = summaries.data?.summaries.find(
    (one) => one.project === project,
  );
  const embeddingRow = embeddings.data?.embeddings.find(
    (one) => one.project === project,
  );
  const path = `/projects/${encodeURIComponent(project)}`;

  if (settings.error !== null) {
    return <ErrorBox message={settings.error} />;
  }
  if (settings.data === null) {
    return <Spinner what="the settings" />;
  }
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
        What the last index run actually wrote. The two differ from the formats
        below whenever the tree or the ignore lines changed since.
      </p>

      <h2>Indexing</h2>
      {schedule.data !== null && <Effective schedule={schedule.data} />}
      <IndexingEditor
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
      <ServerLines rows={summaries.data?.summaries} project={project} />
      {(summaryRow?.skipped ?? 0) > 0 && (
        <p className="token-expired">
          Not processed:
          <NotProcessed
            project={project}
            queue="summaries"
            count={summaryRow?.skipped ?? 0}
            onRetried={summaries.reload}
          />
        </p>
      )}
      <FeatureEditor
        path={`${path}/features/summarize`}
        probePath="/summaries/probe"
        feature={own?.settings?.summarize}
        settled={featured.data?.features.summarize}
        onSaved={() => {
          settings.reload();
          featured.reload();
          summaries.reload();
        }}
      />

      <h2>Embedding</h2>
      <EmbeddingProgress project={project} view={embeddings.data} />
      <ServerLines rows={embeddings.data?.embeddings} project={project} />
      {(embeddingRow?.skipped ?? 0) > 0 && (
        <p className="token-expired">
          Not processed:
          <NotProcessed
            project={project}
            queue="embeddings"
            count={embeddingRow?.skipped ?? 0}
            onRetried={embeddings.reload}
          />
        </p>
      )}
      <FeatureEditor
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

      {!organization && (
        <>
          <h2>Formats</h2>
          <Formats
            project={project}
            formats={own?.formats ?? []}
            at={own?.formats_at ?? null}
            onUpdated={settings.reload}
          />
        </>
      )}

      <h2>Ignore</h2>
      <Level
        project={project}
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
        inherited={settings.data.inherited}
        onSaved={() => {
          settings.reload();
        }}
      />
    </>
  );
}

/** The formats every index run adds to, and the button that adds without one. */
function Formats({
  project,
  formats,
  at,
  onUpdated,
}: {
  project: string;
  formats: string[];
  at: string | null;
  onUpdated: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function update() {
    setBusy(true);
    setError(null);
    try {
      await post(`/projects/${encodeURIComponent(project)}/formats`, {});
      onUpdated();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {error !== null && <ErrorBox message={error} />}
      {formats.length === 0 ? (
        <Empty>
          No formats recorded yet. An index run or the button fills this in.
        </Empty>
      ) : (
        <div className="chips">
          {formats.map((format) => (
            <span key={format} className="chip static">
              {format}
            </span>
          ))}
        </div>
      )}
      <div className="row">
        <button type="button" disabled={busy} onClick={() => void update()}>
          Update formats
        </button>
        <span className="muted">
          Every supported format found in the tree, scripts by their shebang.
          Updated {at === null ? "never" : new Date(at).toLocaleString("en-GB")}
          .
        </span>
      </div>
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
          <Count value={waiting} /> file(s) queued.
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
            Watching the tree: indexed only when it changes, at most once every{" "}
            {minutes(schedule.debounce_minutes)}.
          </>
        )}{" "}
        <span className="muted">
          Last run {when(schedule.last_run)}
          {schedule.next_run !== null && (
            <> - next run {when(schedule.next_run)}</>
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

/** This level's ignore lines, added to every level above it. */
function Level({
  project,
  heading,
  level,
  inherited,
  onSaved,
}: {
  project: string;
  heading: React.ReactNode;
  level: SettingsLevel | null;
  inherited: InheritedIgnore[];
  onSaved: () => void;
}) {
  const path = `/projects/${encodeURIComponent(project)}`;
  const draft = useDraft(`${path}/ignore`, {
    ignore_patterns: level?.ignore_patterns ?? "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(document: string) {
    setBusy(true);
    setError(null);
    try {
      await put(`${path}/settings`, { ignore_patterns: document });
      if (document === "") {
        draft.discard();
      } else {
        draft.commit();
      }
      onSaved();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="level">
      <h3>{heading}</h3>
      {error !== null && <ErrorBox message={error} />}
      {inherited.map((one) => (
        <label key={`${one.origin}:${one.name}`}>
          Inherited from{" "}
          {one.origin === "global" ? "the global default" : one.name}
          <pre className="report">{one.document}</pre>
        </label>
      ))}
      <label>
        Pruned here as well, on top of the built-in skip list and key material.
        <textarea
          className={unsaved(undefined, draft.isDirty("ignore_patterns"))}
          value={draft.value.ignore_patterns}
          rows={12}
          onChange={(event) =>
            draft.update({ ignore_patterns: event.target.value })
          }
        />
      </label>
      <div className="row">
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() => void save("")}
        >
          Clear
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void save(draft.value.ignore_patterns)}
        >
          Save
        </button>
        <UnsavedNote
          count={draft.dirtyCount}
          disabled={busy}
          onDiscard={draft.discard}
        />
      </div>
    </div>
  );
}
