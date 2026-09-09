export type IndexJob = {
  // Null on the fold an organization answers with: it holds the runs of every
  // project under it rather than being one.
  id: number | null;
  project: string;
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

// Where the last index run read one half of the selection from: "file" for a
// .enggraph-keep still in the tree, "project" / "organization" / "global" for
// a stored row, "default" for the built-in set. Null until first indexed.
export type SelectionOrigin =
  | "file"
  | "project"
  | "organization"
  | "global"
  | "default";

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
    root_path: string;
    mounted: boolean;
    // The last index run of this member, and how long ago it started. Both
    // null until it has been indexed once.
    indexed_at: string | null;
    stale_seconds: number | null;
    // What this member does on its own, resolved through the organizations
    // holding it, so `origin` is the level a reader would go and edit.
    schedule: ScheduleSummary;
  }[];
};

export type Memberships = {
  project: string;
  organizations: string[];
  // The organization the project was moved into, which is where it is listed.
  // Null when every membership is a reference it was added by.
  owner: string | null;
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

// One level of the selection: the project, an organization holding it, or
// the global default. Either document may be null, which is that level
// declining to speak for it and letting the level above answer.
export type SettingsLevel = {
  ctxkeep: string | null;
  ctxignore: string | null;
  settings: LevelSettings | null;
  updated_at: string | null;
};

// When a project indexes itself, resolved through every level. `origins`
// names the level each field came from: they are settled one at a time, so a
// project may set `auto` while the interval behind it is still the global
// one. Answered by the API, which is where that resolution is implemented.
export type ProjectSchedule = ScheduleSummary & {
  origins: Record<string, string>;
  last_run: string | null;
  next_run: string | null;
  scheduler: boolean;
};

// The project's own row of the selection, beside where the last run read each
// half of it from.
export type SettingsSource = SettingsLevel & {
  root_path: string;
  keep_source: SelectionOrigin | null;
  ignore_source: SelectionOrigin | null;
};

export type ProjectSettings = {
  project: SettingsSource | null;
  global: SettingsLevel | null;
};

// What a scan of a project's tree proposes, and what it would select.
export type ScanResult = {
  project: string;
  ctxkeep: string;
  ctxignore: string;
  report: string;
};

export type FileType = {
  extension: string;
  count: number;
};

// The schedule of a project as a listing carries it. A row of it is null when
// the API could not be reached, which is not the same answer as `off`.
export type ScheduleSummary = {
  project: string;
  mode: string;
  interval_minutes: number;
  debounce_minutes: number;
  watched: boolean;
  origin: string;
};

export type Project = {
  name: string;
  type: string;
  // What the project is for, in a sentence, written by hand on this page.
  // Null until somebody writes one.
  description: string | null;
  // The host tree it reads. An organization reads none of its own and
  // carries the synthetic `registered://<name>` instead.
  root_path: string;
  keep_source: SelectionOrigin | null;
  ignore_source: SelectionOrigin | null;
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
// which asks for the level each field came from as well.
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
