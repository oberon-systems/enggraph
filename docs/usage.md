---
layout: default
title: Usage
nav_order: 8
---

## Essential commands

```text
make init        create the virtualenv and install the pre-commit hooks
make install     onboard AGENT_ROOT=<path> onto the stack and register it
make reregister  rewrite every onboarded codebase's agent configuration
make lint        run every pre-commit hook over every file
make build       build every service image
make up          start postgres, mcp-server, the viewer and the dashboard
make down        stop the stack, keeping the database volume
make mounts      rewrite the compose override from the projects table
make summarize   describe PROJECT's files with the model (BG=1 detaches)
make backup      write the database, or one project, to a file
make restore     put a backup file back
make status      show whether the stack runs and whether anything uses it
make psql        open a psql session against the context database
make clean       remove containers, the database directory and the built images
```

Run `make` with no target for the full list, including per-service
subdivisions (`make graphify build`, `make mcp typecheck`).

`make status` prints the running services, the `/health` payload, and the
node count. Its `sessions` field is the count of connected MCP clients - a
healthy stack reporting `0` means the client never attached, a different
problem from the stack being down.

## MCP tools

| Tool                       | Arguments                                                                                          | Returns                                                                                               |
| -------------------------- | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `describe_project`         | optional `project`, `path`                                                                         | What a project is: type, description, the tree it reads, and for an organization the members it holds |
| `get_code_graph_neighbors` | `node_id`                                                                                          | Incoming and outgoing edges of a node, with the relation type                                         |
| `search_code_nodes`        | `query`, optional `project`, `project_type`, `limit`                                               | Nodes whose name or id matches, in one project, a whole kind, or every member of an organization      |
| `search_code`              | `query`, optional `project`, `project_type`, `limit`                                               | Files whose text or name answers the question, ranked, with the line range to read                    |
| `shortest_path`            | `source_id`, `target_id`, optional `max_hops`                                                      | Shortest chain of relations between two nodes                                                         |
| `save_node_summary`        | `node_id`, `summary`                                                                               | Saves or updates a summary for a specific node                                                        |
| `get_node_summary`         | `node_id`                                                                                          | Retrieves summary, file path and type for a node                                                      |
| `save_plan`                | `plan_id`, `title`, `content`, optional `project`, `status`, `type`                                | Creates or updates a persistent plan; `project: "*"` makes it global                                  |
| `get_plans`                | optional `project`, `status`, `type`                                                               | Plans of one project plus the global ones; `project: "*"` lists all                                   |
| `drop_plan`                | `plan_id`                                                                                          | Deletes one plan outright, for one written by mistake                                                 |
| `drop_project`             | `name`, optional `confirm`                                                                         | Reports what dropping a project costs, and drops it on `confirm: true`                                |
| `save_memory`              | `memory_id`, `title`, `text`, optional `about`, `summary`, `tags`                                  | Writes a memory into `_memory`; `about: "*"` makes it global                                          |
| `get_memory`               | optional `memory_id`, `about`, `tags`, `query`, `limit`                                            | Memories of one scope plus the global ones, in full                                                   |
| `drop_memory`              | `memory_id`, optional `about`                                                                      | Deletes one memory that turned out to be wrong                                                        |
| `save_suggestion`          | `suggestion_id`, `title`, `detail`, optional `about`, `summary`, `kind`, `lever`, `status`, `bump` | Records a gap in `_suggestions`; saving under an existing slug counts a hit rather than duplicating   |
| `get_suggestions`          | optional `suggestion_id`, `about`, `status`, `kind`, `query`, `limit`                              | Open gaps of one scope plus the global ones, most often hit first                                     |
| `drop_suggestion`          | `suggestion_id`, optional `about`                                                                  | Deletes one suggestion written by mistake; a closed gap is retired instead                            |
| `list_indexed_files`       | optional `project`                                                                                 | The files tracked in `file_hashes`, which is the parser half of the tree                              |
| `get_file_hash`            | `file_path`, optional `project`                                                                    | The stored hash of one file, or nothing when it was never indexed                                     |
| `set_file_hash`            | `file_path`, `hash`, optional `project`                                                            | Writes a file's hash, marking it indexed                                                              |
| `clear_file_hash`          | `file_path`, optional `project`                                                                    | Forgets a file's hash, so the next run re-parses it                                                   |

Example - find how two pieces of code are related:

```text
search_code_nodes(query: "SummaryStore")
shortest_path(source_id: "src/index.ts::handleRequest", target_id: "src/storage.py::SummaryStore")
```

Errors come back as a tool result with `isError` set, rather than tearing
down the client session.

## Searching by meaning

`search_code_nodes` matches identifiers. `search_code` answers a question
asked in words, by fusing that match with a vector search over the text of the
indexed files:

```text
search_code(query: "where does a claimed batch go back to the queue")
```

Each row names the file and the line range to read, so the answer is a place
to open rather than a node id to look up further. The two halves are ranked
separately and combined, which is why a file both halves found outranks one
that only the vector half did.

The vector half needs the files embedded. Nothing is embedded until embedding
is switched on for the project in the dashboard settings, and until then
`search_code` answers with its lexical half alone and says so in the reply.
See [Embedding and the vector half](#embedding-and-the-vector-half).

## Project types and searching across them

Every project carries a type: `codebase` (the default), `docs` and `config`
for indexed trees, and `memory` for the built-in `_memory` project. It is set
by the type given at onboarding and stored once - a later run without
`TYPE=` keeps it rather than resetting it.

The type earns its place in `search_code_nodes`, which is the one read that
need not stop at a project:

```text
search_code_nodes(query: "retention", project: "*")
search_code_nodes(query: "retention", project_type: "docs")
```

The first searches every graph in the database, the second every project of
one kind. Each row names its project, and the limit is shared out between the
projects rather than spent on whichever sorts first, so six indexed codebases
answer with six projects' hits. A named `project` and a `project_type` cannot
be combined - one narrows what the other spans.

## Embedding and the vector half

Vectors are written by a queue rather than by an index run: indexing is fast
and a model is not, so a file is queued when its hash moves and embedded in
the background afterwards. The queue does nothing until the feature is
switched on.

Switch it on in the dashboard, on the settings page for every project at once
or on a project's own settings tab. The global level has two switches:
enabled/disabled is the kill switch - disabled is off everywhere, whatever a
project says, so the model behind it can be shut down without visiting each
project first - and status on/off is only what a project that says nothing
about itself does. A project may state the opposite and it wins, which is how
one project is embedded and no others.

The model runs as a server. The primary is the URL stored in the settings, or
`EMBED_SERVER_URL` (a `llama-server` on a machine with a GPU) when none is
stored, and the queue only ever uses the primary: with it down, the queue
waits. `EMBED_LOCAL_URL` (the `embedder` container beside the stack, on CPU)
answers search queries when the primary cannot be reached. A GPU is not
required: store `http://embedder:8080` as the URL and the container embeds the
queue too. Start the bundled one with the weights and the profile:

```bash
make llm-model-install MODEL=nomic-embed
make up EMBED=1
```

To fill a large tree in one go rather than waiting for the queue:

```bash
make embed PROJECT_NAME=alpha
```

Each feature is one row on the settings page - the switch, the server URL and
a button that reads Test until the address answers, then Save. An empty
field inherits from the level above, and the tab shows in grey what it
inherits and from which level; emptying a field and saving clears that field
alone. A project's settings tab reports how many of its files have vectors,
how many are queued, and which level decided the switch. The three states of
that switch, what the first pass costs without a GPU, how the queue handles a
server that is not there, and what changing the model would mean are all on
the [embedding](https://oberon-systems.github.io/enggraph/embedding.html)
page.

## Organizations

An `organization` is a project that holds other projects rather than a tree of
its own. Its own graph is empty by design, so every read naming it covers the
projects it holds instead, and each row says which member answered:

```text
describe_project()
search_code_nodes(query: "retention", project: "acme")
get_node_summary(node_id: "README.md", project: "acme")
```

`describe_project` is where a session starts. It says whether the project is an
organization and, when it is, lists the members with the sentence written about
each one - which is what an agent picks by. Given a `path` it answers for
whichever project reads that directory instead, so a session works out which
project it is standing in rather than assuming:

```text
describe_project(path: "/home/me/src/monorepo/services/api")
```

The plans, memories and suggestions of an organization are read as a set the
same way: a read naming it returns its own records, every member's, and the
global ones. Writing is the exception. A record saved about an organization
belongs to the organization, and a summary or a file hash is refused outright -
those belong to a graph, and an organization has none, so the call names the
member to write to.

A project is renamed on its own page, under its title, and the rename asks for
the current name first. Rows are re-keyed where they stand: the graph, the
settings, the memberships and the records written about the old name all
follow it, and nothing is indexed again. Two things do not follow,
because they are not in the database - run `make mounts` and restart the
services, and point any onboarded codebase's `.mcp.json` at the new
`/mcp/<name>`.

The sentence each member is described by is written on that project's own page
in the dashboard, under its name. It is at most 500 characters: what a project
is at length is what its README is for.

## One project, one tree

A project is one host tree, mounted read-only at `/code/<project>`, and every
node id is a path relative to it:

```text
list_projects()
search_code_nodes(query: "nginx.conf", project: "configs")
get_node_summary(node_id: "prod/nginx.conf")
```

`list_projects` carries a `root_path` field naming the host tree each project
reads, so a lookup knows which project a path belongs to. An organization
reads none of its own and carries `registered://<name>` there instead.

Both halves of the graph are built in one pass over the tree, so a call from
an infrastructure file resolves into a definition in a source file rather than
becoming an external placeholder.

## Memory

What an agent works out about a repository - a convention, a decision, why
something is the way it is - has nowhere to live in a tree it may only read.
`save_memory` writes it into `_memory`, a built-in project of type `memory`
created by the migration, holding records rather than files:

```text
save_memory(memory_id: "commit-style", title: "Commits go through cz",
            text: "...", about: "*", tags: ["git"])
get_memory(about: "api")
drop_memory(memory_id: "api/commit-style")
```

A memory is tagged with what it is about, the way a plan is - a project name,
or `"*"` for one belonging to no repository in particular - and reading one
scope always returns the global ones alongside it. Nothing indexes into
`_memory`: indexing refuses a project name starting with `_`, so a memory
is never pruned by a re-index the way a derived node is. It is also not a
plan: a memory is what stays true after the task, a plan is what to do next.

## Suggestions

The other half of a memory. A memory says what is true about a codebase; a
suggestion says what the tools could not tell you about it - a lookup that came
back empty, a summary too thin to answer from, a file type no parser reads.
`save_suggestion` writes it into `_suggestions`, a built-in project of type
`suggestions`, holding records the same way `_memory` does:

```text
save_suggestion(suggestion_id: "hcl-no-parser",
                title: "No parser reads *.hcl",
                detail: "...", kind: "no-parser", lever: "coverage")
get_suggestions(about: "zeta")
save_suggestion(suggestion_id: "hcl-no-parser", title: "...", detail: "...",
                status: "resolved", bump: false)
```

The `suggestion_id` is a stable slug derived from the gap, and that is the
whole mechanism: saving under one that exists is how the same gap is reported
again. The record keeps its `first_seen`, moves its `last_seen`, increments
`hits`, and keeps any `kind` or `lever` the call did not name. It also reopens
a suggestion that had been resolved, because a gap hit again is not a resolved
one. `bump: false` corrects the wording or the status without claiming a fresh
sighting, which is what retiring one looks like.

Three vocabularies, free text in the database and documented rather than
`CHECK`-enforced, like a plan's status:

- `kind` - `empty-lookup`, `missing-summary`, `thin-summary`, `not-indexed`,
  `no-parser`, `stale-index`, `missing-tool`.
- `lever` - what closing the gap buys: `tokens` when the answer was re-derived
  by hand, `coverage` when the graph does not describe it at all, `runtime`
  when it was answerable but slow.
- `status` - `open` (the default), `resolved`, `wontfix`.

Reads default to the open ones of the named scope plus the global ones, most
often hit first; `status: "*"` reads every status. `drop_suggestion` erases the
count a record accumulated, so it is for one written by mistake - a gap that
has since been closed is retired with `status: "resolved"` instead. Because
`_suggestions` is a project like any other, `search_code_nodes` with
`project_type: "suggestions"` finds them, and the dashboard lists them on its
own tab.

## Plans

A plan is not derived from a tree, so it is not owned by a project: every
plan is a node of the built-in `_plans` project, keyed on a `plan_id` unique
across the database, with `project` a free-text tag in the node's metadata
rather than a foreign key. Consequences worth knowing:

- A plan survives a project drop and `drop_project`, and can name a
  repository this database has never indexed.
- `get_plans` defaults to the connected project plus the global ones;
  `project: "*"` lists every project's plans at once.
- `save_plan` with `project: "*"` stores a plan that belongs to no project -
  the right home for a procedure run on demand rather than finished once.

`type` is a separate axis from `status`: `status` (`active`, `completed`,
`archived`) says where a plan stands, `type` (`plan`, `template`,
`procedure`) says what kind of record it is. `get_plans` defaults to
`type: "plan"`, so a reusable procedure never shows up mixed in with
pending work; ask for `type: "*"` to see everything. Re-saving a plan
without naming a `type` resets it to `plan`, the same way omitting `status`
resets it to `active` - `save_plan` writes a whole row, it does not patch
one.

## Queues

Summarizing and embedding both fill a queue and drain it in the background.
The **Queues** page puts the two side by side: a percent for each, and a row
per project saying how much is described, how much has vectors, and how much
each still owes. Failures are called out because nothing retries them once a
file has used up its attempts.

```bash
curl -s http://127.0.0.1:3000/api/summaries | python3 -m json.tool
curl -s http://127.0.0.1:3000/api/embeddings | python3 -m json.tool
```

## Web interface

`make up` serves a dashboard at <http://localhost:3000>, on the stack's one
entry point. It's the one service here that writes to the database on a
browser's behalf and it has no authentication of its own, so anything that
can reach the entry point can edit what it shows.

- **Projects** - what's indexed, where it came from, node/edge/file/plan
  counts, and how stale the index is. Three tabs: _Indexed_ for the trees,
  _Organizations_ for the projects that hold other projects, and _System_ for
  the built-in ones holding what an agent wrote. Searchable
  by name or path, sortable by every count and by freshness, with the type
  staged in place and applied only when the change is confirmed, a `When`
  column naming the schedule each project resolves to and a `Sel` column
  saying whether the last index run read its selection from here or from a
  file left in the tree. _New project_ registers one, with a host path or
  without: a project that reads no tree is an organization, which is what
  other projects are moved into.
- **A project** - five tabs: _overview_ (node type breakdown, the members it
  holds when it is an organization - each with its own settings, index and
  drop buttons, how it is kept indexed and how long ago it last ran - and the
  organizations holding it), _graph_ (the
  viewer's page, proxied so the frame shares this origin), _nodes_ (search
  and inspect one node's summary, metadata, neighbours and stored source),
  _files_ (file nodes with entity counts and hash status), _settings_ (when
  it is indexed, and what it indexes). Under the title: the sentence saying
  what the project is for, a _Rename_ that asks for the current name first,
  and _Drop project_.
- **Plans** - every plan in the database, filterable by project, status,
  type or a text search; opens as rendered markdown and edits in place.
- **Suggestions** - the recorded gaps, most often hit first, filterable by
  the project they are about, status or kind. It triages rather than
  authors: the status, the wording and the vocabularies are editable, the
  hit count and the first sighting are not, and there is no way to create
  one here - a suggestion is written by the agent that hit the gap.
- **Settings** - what every project falls back to when neither it nor an
  organization holding it has said otherwise: the indexing schedule, and the
  selection.

### Indexing on a schedule

A project is indexed by hand until something says otherwise, and what says
otherwise is a mode stored beside the selection, at the same three levels:

| Mode       | What it does                                                  |
| ---------- | ------------------------------------------------------------- |
| `off`      | nothing runs on its own; the Index button is the only trigger |
| `periodic` | a run every N minutes                                         |
| `auto`     | a run when a file changes, throttled, and the timer as well   |

`auto` watches the mounted tree with inotify and starts a run once it has
been quiet for the throttle. The timer stays under it as a fallback, because
a watch can be blind and say nothing about it: a tree on a network filesystem
delivers no events at all, and a watch the host refuses for want of
`fs.inotify.max_user_watches` is one the container cannot raise.

Every field is resolved on its own, so a project may set `auto` while the
interval behind its fallback sweep is still the global one. The `When` column
on the projects list shows the mode each project resolves to, and the settings
tab states the whole result above the fields.

An organization indexes everything under it. Its Index button starts a run for
every project it holds, skipping the members set to `off` - `off` is a project
saying it is indexed by hand, and the button on its own page still does that.
A member already indexing is reported as skipped rather than as a failure, and
the page shows the runs folded into one: still going while any of them is,
failed with each failure naming its project.

Two runs of one project never overlap: an index run holds a row, and a second
start is refused while the first is going, whether it came from the schedule
or from the button.

Registering a project writes a row and mounts nothing: the
compose override is a file on the host and both services hold the mounts they
started with, so `make mounts` there is what finishes the job. The dashboard
says so in its own reply rather than leaving it to be discovered.

A drop reports what it will cost before it happens and asks the project name
to be typed as confirmation. Dropping a directory is not a drop of what it
indexed - those nodes go on the next index run, the same way a deleted file's
do.
