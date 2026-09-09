export type IndexJob = {
  // Null on the fold an organization answers with: it holds the runs of every
  // project under it rather than being one.
  id: number | null;
  project: string;
  // Which directories the run walked, or null for every one of them.
  aliases: string[] | null;
  status: "running" | "done" | "failed";
  files: number | null;
  with_node: number | null;
  entities: number | null;
  edges: number | null;
  failures: number | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  // Present on the fold: one entry per project it covered, and the runs that
  // were not started because that project is already indexing.
  runs?: IndexJob[];
  skipped?: { project: string; why: string }[];
};

export type ProjectSource = {
  alias: string;
  root_path: string;
  // Where the last index run read this directory's selection from: "file" for
  // a .ctxkeep still in the tree, "directory" / "project" / "global" for a
  // stored row, "default" for the built-in set. Null until first indexed.
  keep_source: string | null;
  ignore_source: string | null;
};

// What a merge moved, as the API reports it back. `was` is the alias the
// directory carried in the project it came from, empty for a tree mounted
// whole, and the records are the ones whose scope followed the name.
export type Absorbed = {
  sources: { alias: string; root_path: string; was: string }[];
  memories: number;
  plans: number;
  suggestions: number;
};

// What a project reads, as the worker API reports it back after a change:
// only whether the host has each directory mounted, not the selection.
export type MountedSource = {
  alias: string;
  root_path: string;
  mounted: boolean;
};

export type AbsorbAnswer = {
  project: string;
  sources: MountedSource[];
  absorbed: Absorbed;
  mounts?: string;
};

// What an organization holds. A member is a project in its own right: it keeps
// its name, its address and its graph, and appears here by reference.
export type Members = {
  project: string;
  // `owned` is a project moved into this organization, which is where it is
  // listed; the others were added to it and are listed as their own.
  members: {
    project: string;
    owned: boolean;
    description: string | null;
    sources: MountedSource[];
  }[];
};

export type Memberships = {
  project: string;
  organizations: string[];
  // The organization the project was moved into, which is where it is listed.
  // Null when every membership is a reference it was added by.
  owner: string | null;
};

// One directory that changed hands. `was` is the alias it carried before and
// `left` the project it came from; a detach reports the project it made.
// `dropped` says that directory was the last one its project had, so the
// project moved rather than a directory of it, and its name is gone along
// with the records that named it.
export type SourceMoved = {
  project: string;
  alias: string;
  root_path: string;
  was: string;
  left: string;
  dropped: boolean;
  memories: number;
  plans: number;
  suggestions: number;
};

export type MoveAnswer = {
  project: string;
  sources: MountedSource[];
  target: { project: string; sources: MountedSource[] };
  moved: SourceMoved;
  mounts?: string;
};

// When a project indexes itself, as one level states it. Every field may be
// absent, which is that level inheriting it from the one above.
export type Indexing = {
  mode?: string;
  interval_minutes?: number;
  debounce_minutes?: number;
};

// Everything a level says apart from the two selection documents, which are
// columns of their own. One JSONB object, so the next knob is a key.
export type LevelSettings = {
  indexing?: Indexing;
};

// One level of the selection: a directory, the project, or the global
// default. Either document may be null, which is that level declining to
// speak for it and letting the level above answer.
export type SettingsLevel = {
  ctxkeep: string | null;
  ctxignore: string | null;
  settings: LevelSettings | null;
  updated_at: string | null;
};

// What a project's directories come to once folded into the single run they
// share: the most eager of them decides, and only those in `auto` are
// watched. Answered by the API, which is where that fold is implemented.
export type ProjectSchedule = {
  project: string;
  mode: string;
  interval_minutes: number;
  debounce_minutes: number;
  watched: string[];
  levels: {
    alias: string;
    mode: string;
    interval_minutes: number;
    debounce_minutes: number;
    origins: Record<string, string>;
  }[];
  last_run: string | null;
  next_run: string | null;
  scheduler: boolean;
};

export type SettingsSource = ProjectSource & SettingsLevel;

export type ProjectSettings = {
  sources: SettingsSource[];
  project: SettingsLevel | null;
  global: SettingsLevel | null;
};

// What a scan of one directory proposes, and what that proposal would select.
export type ScanResult = {
  project: string;
  alias: string;
  ctxkeep: string;
  ctxignore: string;
  report: string;
};

export type FileType = {
  extension: string;
  count: number;
};

// The folded schedule of a project as the listing carries it. Null when the
// API could not be reached, which is not the same answer as `off`.
export type ScheduleSummary = {
  project: string;
  mode: string;
  interval_minutes: number;
  debounce_minutes: number;
  watched: number;
  origin: string;
};

export type Project = {
  name: string;
  type: string;
  // What the project is for, in a sentence, written by hand on this page.
  // Null until somebody writes one.
  description: string | null;
  root_path: string;
  // What the project reads. One entry with an empty alias is a project
  // mounted whole; several named ones are the slices it was assembled from,
  // and each alias opens every node id that directory produced.
  sources: ProjectSource[];
  indexed_at: string | null;
  stale_seconds: number | null;
  nodes: number;
  edges: number;
  files: number;
  plans: number;
  // How many projects this one holds, which is only ever more than zero
  // for an organization.
  members: number;
};

// Only the listing carries a schedule: the project page has the settings tab,
// which asks for the unfolded levels as well.
export type ProjectListing = Project & {
  schedule: ScheduleSummary | null;
};

export type ProjectDetail = Project & {
  types: { type: string; count: number }[];
  relations: { relation_type: string; count: number }[];
  manual_summaries: number;
  summarised: number;
  hashed_files: number;
  embeddings: number;
};

export type DropReport = {
  name: string;
  root_path: string;
  indexed_at: string | null;
  nodes: number;
  edges: number;
  hashes: number;
  embeddings: number;
  plans: number;
  suggestions: number;
  summaries: number;
  dropped: boolean;
};

export type NodeRow = {
  id: string;
  name: string;
  type: string;
  file_path: string | null;
  summary: string | null;
};

export type NodeDetail = NodeRow & {
  metadata: Record<string, unknown>;
  created_at: string;
  content: string | null;
  content_length: number;
  content_truncated: boolean;
};

export type Neighbor = {
  node_id: string;
  relation_type: string;
  direction: "incoming" | "outgoing";
  type: string | null;
  file_path: string | null;
  summary: string | null;
};

export type FileRow = {
  id: string;
  file_path: string | null;
  summary: string | null;
  entities: number;
  hash: string | null;
  hash_updated_at: string | null;
};

export type PlanRow = {
  id: string;
  project: string | null;
  title: string;
  status: string;
  type: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  content_length: number;
};

export type Plan = Omit<PlanRow, "content_length"> & { content: string };

export type PlanFacets = {
  projects: string[];
  statuses: string[];
  types: string[];
  global_plans: number;
};

export type Page<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
};

export type SuggestionRow = {
  id: string;
  title: string;
  summary: string | null;
  about: string | null;
  kind: string | null;
  lever: string | null;
  status: string;
  hits: number;
  first_seen: string | null;
  last_seen: string | null;
  created_at: string;
  detail_length: number;
};

export type Suggestion = Omit<SuggestionRow, "detail_length"> & {
  detail: string;
};

export type SuggestionFacets = {
  abouts: string[];
  statuses: string[];
  kinds: string[];
  global_suggestions: number;
};
