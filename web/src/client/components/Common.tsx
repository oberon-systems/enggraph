import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo, useState } from "react";

// One path each, drawn on a 16x16 grid: a directory sent past a boundary, one
// pulled back out of it, and one a project stops reading.
export const ICONS = {
  move: "M2 8h8M7 5l3 3-3 3M13 3v10",
  detach: "M14 8H6M9 5L6 8l3 3M3 3v10",
  drop: "M4 4l8 8M12 4l-8 8",
  settings:
    "M8 5.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5M8 1.5v2M8 12.5v2M2.5 8h2M11.5 8h2M4.1 4.1l1.4 1.4M10.5 10.5l1.4 1.4M11.9 4.1l-1.4 1.4M5.5 10.5l-1.4 1.4",
  index: "M4 3l8 5-8 5z",
  fresh: "M13 8a5 5 0 11-1.7-3.8M13 2v3h-3",
  running: "M8 2.5a5.5 5.5 0 105.5 5.5",
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
export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="error">
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

/** An error, opened as a page of its own rather than shown on hover.
 *
 * Whatever failed is usually long - a traceback, a path, a command to run -
 * and the caller that failed is usually an icon in a table cell. So the icon
 * opens this, and this is where the text is read and copied from.
 */
export function ErrorModal({
  title,
  message,
  onClose,
}: {
  title: string;
  message: string;
  onClose: () => void;
}) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <h2>{title}</h2>
        <ErrorBox message={message} />
        <div className="row">
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
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
  directory: "DB",
  project: "DB",
  organization: "DB",
  global: "DB",
  default: "none",
};

const SELECTION_TITLES: Record<string, string> = {
  file: "a .ctxkeep or .ctxignore in the tree, which beats every stored row",
  directory: "stored here, on the directory",
  project: "stored here, on the project",
  organization: "stored here, on an organization this project is part of",
  global: "stored here, as the global default",
  default: "nothing selected it: the built-in set of file types",
};

// Where a schedule was decided, in the words the settings page uses for it.
const SCHEDULE_LEVELS: Record<string, string> = {
  directory: "set on one of its directories",
  project: "set on the project",
  organization: "set on an organization this project is part of",
  global: "set as the global default",
  default: "nothing set one",
};

// The same levels, for a badge that speaks about one directory rather than
// about the run its project folds into.
const DIRECTORY_LEVELS: Record<string, string> = {
  ...SCHEDULE_LEVELS,
  directory: "set on this directory",
};

// What the badge needs, which a project summary and one directory's own
// schedule both answer. `watched` is the count a project folds to; one
// directory does not have one.
type BadgeSchedule = {
  mode: string;
  interval_minutes: number;
  debounce_minutes: number;
  origin: string;
  watched?: number;
};

/** What a schedule comes to, in the space a table column can spare.
 *
 * The mode alone answers "why has this not reindexed itself", which is the
 * question the column exists for; the numbers and the level it was decided at
 * are the title, and the settings tab spells the rest out.
 */
export function ScheduleBadge({
  schedule,
  scope = "project",
}: {
  schedule: BadgeSchedule | null;
  // Whether this stands for a whole project or for one of its directories.
  // Only the wording of the level differs; the fold does not apply to one.
  scope?: "project" | "directory";
}) {
  if (schedule === null) {
    return (
      <span className="muted" title="the API did not answer; this is not `off`">
        ?
      </span>
    );
  }
  const levels = scope === "directory" ? DIRECTORY_LEVELS : SCHEDULE_LEVELS;
  const level = levels[schedule.origin] ?? schedule.origin;
  const detail =
    schedule.mode === "off"
      ? "only the Index button starts a run"
      : schedule.mode === "periodic"
        ? `a run every ${minutes(schedule.interval_minutes)}`
        : `${
            schedule.watched === undefined
              ? "watched"
              : `watching ${schedule.watched} director${
                  schedule.watched === 1 ? "y" : "ies"
                }`
          }, at most once every ${minutes(
            schedule.debounce_minutes,
          )}, swept every ${minutes(schedule.interval_minutes)}`;
  return (
    <span
      className={`origin schedule-${schedule.mode}`}
      title={`${detail}; ${level}`}
    >
      {schedule.mode}
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
