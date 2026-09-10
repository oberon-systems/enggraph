import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo, useState } from "react";

// One path each, drawn on a 16x16 grid. `level` is a stack, for the level a
// setting was read at; `periodic` is a clock and `auto` an eye, for the two
// ways a project indexes itself without being asked.
export const ICONS = {
  drop: "M4 4l8 8M12 4l-8 8",
  settings:
    "M8 5.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5M8 1.5v2M8 12.5v2M2.5 8h2M11.5 8h2M4.1 4.1l1.4 1.4M10.5 10.5l1.4 1.4M11.9 4.1l-1.4 1.4M5.5 10.5l-1.4 1.4",
  index: "M4 3l8 5-8 5z",
  fresh: "M13 8a5 5 0 11-1.7-3.8M13 2v3h-3",
  retry: "M3 8a5 5 0 101.7-3.8M3 2v3h3",
  running: "M8 2.5a5.5 5.5 0 105.5 5.5",
  level: "M8 2l6 3-6 3-6-3zM2 8l6 3 6-3M2 11.5l6 3 6-3",
  periodic: "M8 2a6 6 0 100 12A6 6 0 008 2M8 4.5V8l2.5 1.5",
  auto: "M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8M8 6a2 2 0 100 4 2 2 0 000-4",
} as const;

export function Icon({ path }: { path: string }) {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** What went wrong, in full and in reach.
 *
 * An error is the one thing on a page nobody can act on from a glance: it is
 * read, and then it is pasted into a shell or a ticket. So it is text on the
 * page, wrapped, selectable, with the button that copies it - never a
 * tooltip, which cannot be selected, does not wrap and is gone on the next
 * mouse move.
 */
export function ErrorBox({
  message,
  what,
}: {
  message: string;
  // What failed, when the box is not next to the thing that did.
  what?: string;
}) {
  return (
    <div className="error">
      <p className="error-title">
        <strong>Error</strong>
        {what === undefined ? "" : ` - ${what}`}
      </p>
      <pre>{message}</pre>
      <CopyButton text={message} />
    </div>
  );
}

export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="secondary copy"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(
          () => setCopied(true),
          () => setCopied(false),
        );
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Spinner({ what }: { what: string }) {
  return <div className="muted">Loading {what}...</div>;
}

export function Count({ value }: { value: number }) {
  return <span className="count">{value.toLocaleString("en-US")}</span>;
}

/**
 * Plan bodies are markdown written by an agent. Rendered rather than shown
 * raw, and sanitized rather than trusted: the database is not a boundary.
 */
export function Markdown({ text }: { text: string }) {
  const html = useMemo(
    () => DOMPurify.sanitize(marked.parse(text, { async: false })),
    [text],
  );
  return (
    <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
  );
}

export function Pager({
  total,
  limit,
  offset,
  onOffset,
}: {
  total: number;
  limit: number;
  offset: number;
  onOffset: (value: number) => void;
}) {
  if (total === 0) {
    return null;
  }
  const first = offset + 1;
  const last = Math.min(offset + limit, total);
  return (
    <div className="pager">
      <button
        type="button"
        disabled={offset === 0}
        onClick={() => onOffset(Math.max(0, offset - limit))}
      >
        Previous
      </button>
      <span className="muted">
        {first.toLocaleString("en-US")}-{last.toLocaleString("en-US")} of{" "}
        {total.toLocaleString("en-US")}
      </span>
      <button
        type="button"
        disabled={last >= total}
        onClick={() => onOffset(offset + limit)}
      >
        Next
      </button>
    </div>
  );
}

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** How long ago, in the coarsest unit that still says something. */
export function age(seconds: number): string {
  if (seconds < MINUTE) {
    return "just now";
  }
  if (seconds < HOUR) {
    return `${Math.floor(seconds / MINUTE)} min ago`;
  }
  if (seconds < DAY) {
    const hours = Math.floor(seconds / HOUR);
    return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  }
  const days = Math.floor(seconds / DAY);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export const STALE_AFTER_DAYS = 7;

/** A duration as the schedule stores it, which is always a count of minutes. */
export function minutes(count: number): string {
  return `${count} minute${count === 1 ? "" : "s"}`;
}

export function Freshness({
  indexedAt,
  staleSeconds,
}: {
  indexedAt: string | null;
  staleSeconds: number | null;
}) {
  if (indexedAt === null || staleSeconds === null) {
    return <span className="stale">never indexed</span>;
  }
  const stale = staleSeconds > STALE_AFTER_DAYS * DAY;
  return (
    <span className={stale ? "stale" : "fresh"} title={indexedAt}>
      {age(staleSeconds)}
    </span>
  );
}

// Where a selection came from, in the space a table column can spare. A file
// still in the tree wins over every stored row, so it is worth seeing at a
// glance which projects are still configured from a repository rather than
// from here - those are the ones whose pair has yet to be deleted.
const SELECTION_LABELS: Record<string, string> = {
  file: "FILE",
  project: "DB",
  organization: "DB",
  global: "DB",
  default: "none",
};

const SELECTION_TITLES: Record<string, string> = {
  file: "a .enggraph-keep or .enggraph-ignore in the tree, which beats every stored row",
  project: "stored here, on the project",
  organization: "stored here, on an organization this project is part of",
  global: "stored here, as the global default",
  default: "nothing selected it: the built-in set of file types",
};

// Where a schedule was decided, in the words the settings page uses for it.
export const SCHEDULE_LEVELS: Record<string, string> = {
  project: "set on the project",
  organization: "set on an organization this project is part of",
  global: "set as the global default",
  default: "nothing set one",
};

// What the badge needs, which every listing of a schedule answers.
type BadgeSchedule = {
  mode: string;
  interval_minutes: number;
  debounce_minutes: number;
  origin: string;
};

/** What one schedule does, in the words a hover has room for. */
export function scheduleDetail(schedule: BadgeSchedule): string {
  if (schedule.mode === "off") {
    return "only the Index button starts a run";
  }
  if (schedule.mode === "periodic") {
    return `a run every ${minutes(schedule.interval_minutes)}`;
  }
  return (
    `watching the tree, at most once every ` +
    `${minutes(schedule.debounce_minutes)}, swept every ` +
    minutes(schedule.interval_minutes)
  );
}

/** What a schedule comes to, in the space a table column can spare.
 *
 * The mode alone answers "why has this not reindexed itself", which is the
 * question the column exists for; the numbers and the level it was decided at
 * are the title, and the settings tab spells the rest out.
 */
export function ScheduleBadge({
  schedule,
}: {
  schedule: BadgeSchedule | null;
}) {
  if (schedule === null) {
    return (
      <span className="muted" title="the API did not answer; this is not `off`">
        ?
      </span>
    );
  }
  const level = SCHEDULE_LEVELS[schedule.origin] ?? schedule.origin;
  return (
    <span
      className={`origin schedule-${schedule.mode}`}
      title={`${scheduleDetail(schedule)}; ${level}`}
    >
      {schedule.mode}
    </span>
  );
}

// What the badge needs, which every listing of an embedding state answers.
type BadgeEmbedding = {
  enabled: boolean;
  gated: boolean;
  files: number;
  indexed_files: number;
  queue: Record<string, number>;
};

/** A share as a whole percent, or null when there is nothing to divide by.
 *
 * Rounded down, and never to 100 while one is still missing: a listing saying
 * "done" over an unfinished queue is the one wrong answer here.
 */
export function percentOf(done: number, total: number): number | null {
  if (total === 0) {
    return null;
  }
  const whole = Math.floor((done / total) * 100);
  return whole === 100 && done < total ? 99 : whole;
}

/** Files still owed work: queued, or held by a worker right now. */
export function waitingOf(queue: Record<string, number>): number {
  return (queue.pending ?? 0) + (queue.running ?? 0) + (queue.leased ?? 0);
}

/** The colour of a queue's percent: done, still working, or stopped short. */
export function coverageTone(
  done: number,
  total: number,
  waiting: number,
  failed: number,
): string | null {
  if (total > 0 && done >= total) {
    return "coverage-done";
  }
  if (waiting > 0) {
    return "coverage-busy";
  }
  return failed > 0 ? "coverage-failed" : null;
}

/** How far one queue has got with one project, as a percent and a fraction.
 *
 * Written once and used by the queues page, a project's own page and the
 * members of an organization: three places asking the same question, and
 * three answers that would otherwise drift apart.
 */
export function Coverage({
  done,
  total,
  muted = false,
  title,
  waiting = 0,
  failed = 0,
}: {
  done: number;
  total: number;
  muted?: boolean;
  title?: string;
  // Files still queued, and files given up on: they decide the colour.
  waiting?: number;
  failed?: number;
}) {
  const percent = percentOf(done, total);
  if (percent === null) {
    return (
      <span className="muted" title={title ?? "nothing to do"}>
        -
      </span>
    );
  }
  const tone = muted
    ? "muted"
    : (coverageTone(done, total, waiting, failed) ??
      "origin embedding-filling");
  return (
    <span title={title}>
      <span className={tone}>{percent}%</span>{" "}
      <span className="muted">
        ({done}/{total})
      </span>
    </span>
  );
}

/** How much of a project has vectors, as a whole percent, or null for none. */
export function embeddedPercent(embedding: BadgeEmbedding): number | null {
  return percentOf(embedding.files, embedding.indexed_files);
}

/** What a project's vectors come to, in the space a table column can spare.
 *
 * The percent answers "is this searchable by meaning yet", which is the
 * question the column exists for. Off is not a number at all: nought percent
 * of a project nobody asked to embed is not news.
 */
export function EmbeddingBadge({
  embedding,
}: {
  embedding: BadgeEmbedding | null;
}) {
  if (embedding === null) {
    return (
      <span className="muted" title="the API did not answer; this is not `off`">
        ?
      </span>
    );
  }
  if (!embedding.enabled) {
    return (
      <span
        className="origin embedding-off"
        title={
          embedding.gated
            ? "the global switch is off, so this project is not asked"
            : "embedding is off for this project"
        }
      >
        off
      </span>
    );
  }
  const percent = embeddedPercent(embedding);
  const waiting =
    (embedding.queue.pending ?? 0) + (embedding.queue.running ?? 0);
  const failed = embedding.queue.failed ?? 0;
  const state = percent === 100 && waiting === 0 ? "done" : "filling";
  return (
    <span
      className={`origin embedding-${state}`}
      title={
        `${embedding.files} of ${embedding.indexed_files} file(s) embedded` +
        (waiting === 0 ? ", nothing queued" : `, ${waiting} queued`) +
        (failed === 0 ? "" : `, ${failed} failed`)
      }
    >
      {percent === null ? "-" : `${percent}%`}
    </span>
  );
}

// How long an indexed project is left before its row starts saying so. A
// project that indexes itself is expected to be minutes old, so the scale
// here is far shorter than the one the projects board uses: an organization
// is asked "is any of this stale", and an hour already is.
const INDEXED_WARN_SECONDS = 30 * MINUTE;
const INDEXED_LATE_SECONDS = HOUR;

/** How long ago a project was indexed, coloured by how long that is.
 *
 * `Freshness` answers the same question on a scale of days, for projects
 * indexed by hand. This one is for a row that says the project indexes itself:
 * there, an hour without a run is the thing worth seeing.
 */
export function IndexedAge({
  indexedAt,
  staleSeconds,
}: {
  indexedAt: string | null;
  staleSeconds: number | null;
}) {
  if (indexedAt === null || staleSeconds === null) {
    return (
      <span className="overdue" title="no index run has finished for it yet">
        never indexed
      </span>
    );
  }
  const grade =
    staleSeconds < INDEXED_WARN_SECONDS
      ? "fresh"
      : staleSeconds < INDEXED_LATE_SECONDS
        ? "stale"
        : "overdue";
  return (
    <span className={grade} title={indexedAt}>
      {age(staleSeconds)}
    </span>
  );
}

export function SelectionBadge({ origin }: { origin: string | null }) {
  if (origin === null) {
    return (
      <span
        className="muted"
        title="never indexed, so nothing read a selection"
      >
        -
      </span>
    );
  }
  const label = SELECTION_LABELS[origin] ?? origin;
  return (
    <span
      className={`origin origin-${label.toLowerCase()}`}
      title={SELECTION_TITLES[origin] ?? origin}
    >
      {label}
    </span>
  );
}
