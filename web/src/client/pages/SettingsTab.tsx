import { useState } from "react";

import { post, put, remove } from "../api.js";
import {
  Count,
  Empty,
  ErrorBox,
  SelectionBadge,
  Spinner,
  minutes,
} from "../components/Common.js";
import { IndexingEditor } from "../components/IndexingFields.js";
import { useApi } from "../hooks/useApi.js";
import type {
  FileType,
  Page,
  ProjectSchedule,
  ProjectSettings,
  ScanResult,
  SettingsLevel,
} from "../types.js";

// The project level is the empty alias, which a URL path cannot carry. `-` is
// what the mount listing already writes for it, so it is what this spells too.
export const PROJECT_LEVEL = "-";

/** What a project indexes, and where that answer comes from.
 *
 * The two documents are the same commented text a `.ctxkeep` and a
 * `.ctxignore` hold, because they are that: a repository that still ships the
 * pair keeps deciding its own index, and what is edited here takes over only
 * once those files are gone.
 */
export function SettingsTab({
  project,
  organization,
  alias,
  onAll,
}: {
  project: string;
  organization: boolean;
  // One directory, when the page was opened for it: its settings are its own
  // view rather than a heading somewhere down the project's. Null is the
  // project itself, which is every level at once.
  alias?: string | null;
  onAll: () => void;
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
  const path = `/projects/${encodeURIComponent(project)}`;

  if (settings.error !== null) {
    return <ErrorBox message={settings.error} />;
  }
  if (settings.data === null) {
    return <Spinner what="the settings" />;
  }
  // A project mounted whole reads one unnamed directory, and that row is the
  // project level: there is no second thing to settle for it.
  const mounted = settings.data.sources.find((one) => one.alias === "");

  if (alias !== null && alias !== undefined) {
    const source = settings.data.sources.find((one) => one.alias === alias);
    if (source === undefined) {
      return (
        <>
          <p>
            <button type="button" className="link" onClick={onAll}>
              All of {project}
            </button>
          </p>
          <Empty>
            {project} reads no directory called <code>{alias}</code>.
          </Empty>
        </>
      );
    }
    return (
      <>
        <p>
          <button type="button" className="link" onClick={onAll}>
            All of {project}
          </button>
        </p>
        <h2>
          <code>{alias}/</code> <span className="path">{source.root_path}</span>
        </h2>
        <p className="muted">
          This directory alone. Anything it does not settle here falls back to
          {organization
            ? " the project, its organizations"
            : " the project"}{" "}
          and then the global default.
        </p>

        <h3>Schedule</h3>
        <IndexingEditor
          path={`${path}/indexing/${encodeURIComponent(alias)}`}
          indexing={source.settings?.indexing}
          onSaved={() => {
            settings.reload();
            schedule.reload();
          }}
        />

        <h3>Selection</h3>
        <Level
          project={project}
          alias={alias}
          heading={
            <>
              <code>{alias}/</code>{" "}
              <span className="path">{source.root_path}</span>
            </>
          }
          level={source}
          origins={[source.keep_source, source.ignore_source]}
          onSaved={() => {
            settings.reload();
          }}
        />
      </>
    );
  }

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

      <h2>Schedule</h2>
      {schedule.data !== null && <Effective schedule={schedule.data} />}
      <IndexingEditor
        key={`project-${settings.data.project?.updated_at ?? "none"}`}
        path={`${path}/indexing/${PROJECT_LEVEL}`}
        indexing={settings.data.project?.settings?.indexing}
        onSaved={() => {
          settings.reload();
          schedule.reload();
        }}
      />

      <h2>Selection</h2>
      {/* One level, this project's own. Every directory it reads settles its
          own from the row that names it, and a member of an organization
          settles its own on its own page. */}
      <Level
        project={project}
        alias={PROJECT_LEVEL}
        heading={
          mounted === undefined ? (
            "Every directory of this project"
          ) : (
            <>
              The whole tree <span className="path">{mounted.root_path}</span>
            </>
          )
        }
        level={settings.data.project}
        origins={
          mounted === undefined
            ? []
            : [mounted.keep_source, mounted.ignore_source]
        }
        onSaved={() => {
          settings.reload();
        }}
      />

      <p className="muted">
        This level and no other. Each directory this project reads settles its
        own, from the row that names it on the overview tab, and falls back to
        what is here; a project belonging to an organization falls back to that
        next, and everything falls back to the global default in the end. A{" "}
        <code>.ctxkeep</code> or <code>.ctxignore</code> still in the tree beats
        every one of them, and goes on doing so until it is deleted.
      </p>
    </>
  );
}

/** What the levels above come to, once folded into the one run they share.
 *
 * The fold is the API's: the most eager directory decides the project, and
 * only the directories in `auto` are watched. Stating it here is what keeps
 * the rule from becoming folklore about a settings page.
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
            Watching {schedule.watched.length} of {schedule.levels.length}{" "}
            director
            {schedule.levels.length === 1 ? "y" : "ies"}: indexed when one of
            them changes, at most once every{" "}
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
 * `alias` is what the route is addressed by, so the project level arrives as
 * `-`; `origins` is empty for a level no single directory answers for.
 */
function Level({
  project,
  alias,
  heading,
  level,
  origins,
  onSaved,
}: {
  project: string;
  alias: string;
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
    <div className="level" id={`alias-${alias === PROJECT_LEVEL ? "" : alias}`}>
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
          A selection file in the tree is deciding this directory. What is saved
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
          disabled={busy || alias === PROJECT_LEVEL}
          title={
            alias === PROJECT_LEVEL
              ? "a scan reads one directory, and this level is every one of them"
              : "propose a selection from the file types this directory holds"
          }
          onClick={() =>
            void run(async () => {
              const scan = await post<ScanResult>(`${path}/scan`, { alias });
              setKeep(scan.ctxkeep);
              setIgnore(scan.ctxignore);
              setReport(scan.report);
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
              await remove(
                `${path}/settings/${encodeURIComponent(alias || PROJECT_LEVEL)}`,
              );
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
              await put(
                `${path}/settings/${encodeURIComponent(alias || PROJECT_LEVEL)}`,
                { ctxkeep: keep, ctxignore: ignore },
              );
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
