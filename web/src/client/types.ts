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
    // How much of it has vectors, and how much of it a model has described.
    // Both resolved through the organization, so a member row answers the
    // same two questions the Queues page does.
    embedding: EmbeddingState;
    summary: SummaryState;
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
  allowed?: boolean;
  mode?: string;
  interval_minutes?: number;
  debounce_minutes?: number;
};

// A background feature at one level: whether it runs, and the server it
// talks to. `enabled` absent is that level declining to answer; at the global
// level `false` is a gate rather than a default, and no lower level is asked.
export type Feature = {
  // Whether the feature may run at all, stored only at the global level.
  // `enabled` beside it is a default a project may state otherwise.
  allowed?: boolean;
  enabled?: boolean;
  server_url?: string;
  // What is left of a stored token once the server has taken it out. The
  // token itself never reaches this side.
  key_set?: boolean;
  key_due?: string | null;
  key_expired?: boolean;
  // How fast the queue behind this feature is worked, and how a file is cut
  // before it is embedded.
  batch?: number;
  tick_seconds?: number;
  budget_seconds?: number;
  chunk_chars?: number;
  chunk_overlap?: number;
};

// A memory as the list carries it: what an agent wrote down, keyed
// `<about>/<slug>` so two repositories can each hold a `commit-style`.
export type MemoryRow = {
  id: string;
  title: string;
  summary: string | null;
  about: string | null;
  tags: string[];
  updated_at: string | null;
  created_at: string;
  text_length: number;
};

export type Memory = {
  id: string;
  title: string;
  summary: string | null;
  text: string;
  about: string | null;
  tags: string[];
  updated_at: string | null;
  created_at: string;
};

export type MemoryFacets = {
  abouts: string[];
  tags: string[];
  global_memories: number;
};

// Everything a level says apart from the two selection documents, which are
// columns of their own. One JSONB object, so the next knob is a key.
export type LevelSettings = {
  indexing?: Indexing & Feature;
  summarize?: Feature;
  embedding?: Feature;
};

// One feature settled for one project, as the API resolved it. `gated` is the
// case the dashboard has to word differently: the project is off because the
// global switch is, not because of anything on its own row.
export type FeatureState = {
  feature: string;
  allowed: boolean;
  enabled: boolean;
  gated: boolean;
  server_url: string;
  origins: Record<string, string>;
  // Three facts about the stored token, and never the token: the dashboard
  // has no authentication of its own, so a secret sent back to it would be
  // readable by anyone who can open the page.
  key_set: boolean;
  key_saved_at: string | null;
  key_due: string | null;
  key_expired: boolean;
  batch: number;
  tick_seconds: number;
  budget_seconds: number;
  chunk_chars: number;
  chunk_overlap: number;
  // What this level would get by saying nothing, and from where.
  inherited?: InheritedFeature;
};

export type InheritedFeature = {
  enabled: boolean;
  server_url: string;
  batch: number;
  tick_seconds: number;
  budget_seconds: number;
  chunk_chars: number;
  chunk_overlap: number;
  origins: Record<string, string>;
};

export type ProjectFeatures = {
  project: string;
  features: Record<string, FeatureState>;
};

// What a project's vectors amount to, and how much is still queued.
export type EmbeddingState = {
  project: string;
  allowed: boolean;
  enabled: boolean;
  gated: boolean;
  origin: string;
  server_url: string;
  key_set: boolean;
  key_expired: boolean;
  urls: string[];
  queue: Record<string, number>;
  chunks: number;
  files: number;
  indexed_files: number;
  // Files with the embed skip bit: given up on, left out of the percent.
  skipped: number;
};

// What one project's summaries amount to, and how much is still queued.
export type SummaryState = {
  project: string;
  allowed: boolean;
  enabled: boolean;
  gated: boolean;
  origin: string;
  server_url: string;
  // Whether an address is set for the push loop. Without one the queue is
  // drained by `make summarize` or a remote worker, and by nothing here.
  pushed: boolean;
  job: number | null;
  queue: Record<string, number>;
  files: number;
  described: number;
  manual: number;
  // Files with the summarize skip bit: given up on, left out of the percent.
  skipped: number;
  key_set: boolean;
  key_expired: boolean;
};

// What each queue gave up on for one project, and why.
export type Failures = {
  project: string;
  summaries: { file_path: string; error: string }[];
  embeddings: { file_path: string; error: string }[];
};

export type SummariesView = {
  summaries: SummaryState[];
  loop: boolean;
};

export type EmbeddingsView = {
  embeddings: EmbeddingState[];
  model: string;
  // The window a file is cut into, in characters. A queue counted in files
  // says nothing about how much work one of them is.
  chunk_chars: number;
  loop: boolean;
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
  embedding: EmbeddingState | null;
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
