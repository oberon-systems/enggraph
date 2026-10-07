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
make test        run every test suite, the stack ones on a throwaway eval stack
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

| Tool                       | Arguments                                                                                                            | Returns                                                                                                      |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `describe_project`         | optional `project`, `path`                                                                                           | What a project is: type, description, the tree it reads, and for an organization the members it holds        |
| `get_code_graph_neighbors` | `node_id`                                                                                                            | Incoming and outgoing edges of a node, with the relation type                                                |
| `search_code_nodes`        | `query`, optional `project`, `project_type`, `limit`                                                                 | Nodes whose name or id matches, in one project, a whole kind, or every member of an organization             |
| `search_code`              | `query`, optional `project`, `project_type`, `limit`                                                                 | Files whose text or name answers the question, ranked, with the line range to read                           |
| `search_text`              | `pattern`, optional `loose`, `regex`, `path`, `project`, `project_type`, `limit`                                     | Every line of the mounted trees containing a string, with file and line, like grep                           |
| `get_context`              | `query`, optional `project`, `project_type`, `token_budget`, `seeds`, `expand`, `include_chunks`, `detail`           | One context packet: the search hits, the graph around them, the relations between them, within budget        |
| `shortest_path`            | `source_id`, `target_id`, optional `max_hops`                                                                        | Shortest chain of relations between two nodes                                                                |
| `find_definition`          | `symbol`, optional `project`, `file_path`                                                                            | Where a symbol is defined: file, line, the class holding it, summary                                         |
| `find_callers`             | `symbol`, optional `project`, `file_path`, `max_hops`                                                                | Graph `calls` edges and call sites matched by name, each with its evidence                                   |
| `find_callees`             | `symbol`, optional `project`, `file_path`, `max_hops`                                                                | What a symbol calls, from graph edges alone                                                                  |
| `find_references`          | `symbol`, optional `project`, `file_path`, `max_hops`                                                                | Every incoming edge but containment, and every mention of the name                                           |
| `find_implementations`     | `symbol`, optional `project`, `file_path`, `max_hops`                                                                | What extends or implements a class or interface                                                              |
| `find_tests`               | `symbol`, optional `project`, `file_path`, `max_hops`                                                                | Test files that reach a symbol within two hops                                                               |
| `impact_analysis`          | `symbol` or a file path, optional `project`, `file_path`, `depth`                                                    | What a change could reach: direct, indirect, tests, public API, configuration                                |
| `get_project_links`        | optional `project`, `direction`, `depth`, `relation`, `node_id`                                                      | Which projects use which and by what, what one provides, and the names left unlinked                         |
| `trace`                    | `node_id`, optional `project`, `max_steps`                                                                           | Follows a node across projects: what provides it, who takes it, what applies them there, what they deploy to |
| `find_linked_name`         | `name`, optional `kind`, `project`, `project_type`                                                                   | Who defines and who uses a host, image or package, by any spelling of its name                               |
| `save_project_link`        | `target_project`, `relation`, optional `project`, `source_id`, `target_id`, `note`                                   | Declares a relation between two projects that no manifest states                                             |
| `drop_project_link`        | `target_project`, `relation`, optional `project`, `source_id`, `target_id`                                           | Removes a relation declared with `save_project_link`                                                         |
| `save_project_export`      | `kind`, `name`, optional `project`, `node_id`                                                                        | Says a project provides a name no file of it states, such as an image CI builds                              |
| `drop_project_export`      | `kind`, `name`, optional `project`                                                                                   | Removes an export declared by hand; one a manifest declares stays                                            |
| `save_node_summary`        | `node_id`, `summary`                                                                                                 | Saves or updates a summary for a specific node                                                               |
| `get_node_summary`         | `node_id`                                                                                                            | Retrieves summary, file path and type for a node                                                             |
| `get_overview`             | optional `project`, `path`, `depth`, `include_entities`, `token_budget`                                              | The summary tree under a directory or file, `./` being the repository, cut to budget                         |
| `save_plan`                | `plan_id`, `title`, `content`, optional `project`, `status`, `type`, `nodes`                                         | Creates or updates a persistent plan; `project: "*"` makes it global                                         |
| `get_plans`                | optional `project`, `status`, `type`, `node_id`, `node_project`                                                      | Plans of one project plus the global ones; `project: "*"` lists all                                          |
| `drop_plan`                | `plan_id`                                                                                                            | Deletes one plan outright, for one written by mistake                                                        |
| `drop_project`             | `name`, optional `confirm`                                                                                           | Reports what dropping a project costs, and drops it on `confirm: true`                                       |
| `save_memory`              | `memory_id`, `title`, `text`, optional `about`, `summary`, `tags`, `nodes`                                           | Writes a memory into `_memory`; `about: "*"` makes it global                                                 |
| `get_memory`               | optional `memory_id`, `about`, `tags`, `query`, `node_id`, `node_project`, `limit`                                   | Memories of one scope plus the global ones, in full                                                          |
| `drop_memory`              | `memory_id`, optional `about`                                                                                        | Deletes one memory that turned out to be wrong                                                               |
| `save_suggestion`          | `suggestion_id`, `title`, `detail`, optional `about`, `summary`, `kind`, `lever`, `status`, `bump`, `query`, `nodes` | Records a gap in `_suggestions`; saving under an existing slug counts a hit rather than duplicating          |
| `get_suggestions`          | optional `suggestion_id`, `about`, `status`, `kind`, `query`, `node_id`, `node_project`, `group_by`, `limit`         | Open gaps of one scope plus the global ones, most often hit first, or rolled up by `group_by`                |
| `drop_suggestion`          | `suggestion_id`, optional `about`                                                                                    | Deletes one suggestion written by mistake; a closed gap is retired instead                                   |
| `list_indexed_files`       | optional `project`                                                                                                   | The files tracked in `indexed_files`, which is the parser half of the tree                                   |
| `get_file_hash`            | `file_path`, optional `project`                                                                                      | The stored hash of one file, or nothing when it was never indexed                                            |
| `set_file_hash`            | `file_path`, `hash`, optional `project`                                                                              | Writes a file's hash, marking it indexed                                                                     |
| `clear_file_hash`          | `file_path`, optional `project`                                                                                      | Forgets a file's hash, so the next run re-parses it                                                          |
| `list_skills`              | optional `project`                                                                                                   | The skills a session should have installed, each with its sha256 version                                     |
| `get_skill`                | `name`, optional `project`                                                                                           | One skill's text, version stamped, and the path to write it to                                               |

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

The combined list is then reranked: a node whose name is a word of the query,
a path that shares words with it, and code rather than a documentation heading
move up. `rerank: false` returns the combined order untouched, which is how a
ranking is compared against it.

The vector half needs the files embedded. Nothing is embedded until embedding
is switched on for the project in the dashboard settings, and until then
`search_code` answers with its lexical half alone and says so in the reply.
See [Embedding and the vector half](#embedding-and-the-vector-half).

## Assembling context in one call

`get_context` is the call to reach for when the question is broad - how a
subsystem works, what a change would touch - rather than where one identifier
lives:

```text
get_context(query: "how does a claimed batch get embedded", token_budget: 12000)
```

It runs the same hybrid search and the same reranker as `search_code`, takes
the top hits as seeds, and then walks the graph around them into tiers: what
depends on the seed, the tests near it, what it defines, what it calls, and
what it imports. A hit is usually a file - the embedded chunks are keyed to
file nodes - so what that file defines is the substance of the match rather
than noise. The result is deduplicated, by node and by overlapping line ranges
in one file, and cut to `token_budget`, which is spent on the search hits
first and then on the tiers in that order.

Each seed may keep only so much of a tier, and the bulk tiers - what a file
defines, what it imports - decay with the rank of the hit that pulled them in.
A map of the best hit's file is context; the same map of the eighth hit is a
table of contents nobody asked for.

Every entry carries `origin`, `search` for a hit and `expansion` for a node
the graph reached, and `why`, the relation that brought it in. `relationships`
lists the edges between the entries that survived the budget, so the packet
says how the pieces connect rather than only which they are.

`expand` sets the hops per tier and switches one off with 0:

```text
get_context(query: "authentication", expand: { callers: 2, defines: 1, tests: 1, imports: 0 })
```

The source text in a packet is read from the mounted tree, for the chunks
`search_code` finds: the database keeps where a chunk is and its words, never
its text. A project with embedding switched off still answers, with
references, summaries and relations alone, and says so in `notes`; so does a
packet when the worker API that reads the trees does not answer. Pass
`include_chunks: false` to ask for that deliberately, which is far cheaper.

The budget is an estimate, at four characters per token: the server holds no
tokenizer, and the callers do not share one.

`detail` decides what the budget is spent on:

| `detail`         | The packet                                                                                                                            |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `source`         | Code first, as above                                                                                                                  |
| `summary`        | Each hit's file and directories up to the repository, what the hits hold, and source for the best two hits only                       |
| `auto` (default) | `source` when the question names code - an identifier, a path, a call - and `summary` otherwise, or when `token_budget` is under 3000 |

The packet says which one ran, in `detail` and in `notes`.

## Summaries at every level

Every node of the ladder repository, directory, file, symbol carries one
sentence. A broad question starts at the top of that ladder and drills down,
and reads source only once the summaries have named the place:

```text
get_overview()
get_overview(path: "src/payments/", depth: 1, include_entities: true)
get_node_summary(node_id: "src/payments/service.ts")
```

A directory id ends in a slash and the repository is `./`. Each item carries
its summary, `summary_source` (`auto`, `llm` or `manual`) and how many
children it has, so a directory cut by the budget is still one call away.

The summaries need no model. Indexing writes an `auto` one for each level; the
model, where one is set up, replaces it with a better one. See
[Summarization](https://oberon-systems.github.io/enggraph/summarization.html).

## Navigating by symbol

The `find_*` tools answer one question about one symbol. The symbol is
written `Class.method`, `function` or `Class`, and `file_path` picks one node
when several share the name:

```text
find_definition(symbol: "PaymentService.refund")
find_callers(symbol: "PaymentService.refund")
find_tests(symbol: "RetryPolicy")
impact_analysis(symbol: "src/config.ts")
```

Every result carries `evidence`. `graph` is an edge, with its relation and the
extractor's confidence. `text` is the name matched as a whole word in another
indexed file, read from its mounted tree, marked `NAME_MATCH`, with the lines
it was found on.

Two limits shape the answers. Calls across files are graph edges for Python,
TypeScript and JavaScript only; in other languages a caller in another file is
found by text alone. The text half searches the mounted trees through the
worker API, embedding or not; when the worker API does not answer, the
results are graph edges only and `notes` says so.

A string the graph does not name - a host, a key, an error message - is found
with `search_text`, the way grep finds it: every line of the indexed trees
containing it, with the file and the line, unranked.

```text
search_text(pattern: "web-01.example.com", project: "*")
search_text(pattern: "web_01_example_com", loose: true)
search_text(pattern: "retry_(count|limit)", regex: true, path: "**/*.py")
```

`loose` ignores what separates the words of the pattern, `regex` takes it as
a regular expression, and `path` narrows the files by a glob. The trees are
read as the indexer reads them: the ignore lines, the default skip list and
the deny list leave the same files out. Projects are read in turn until the
limit is met.

`impact_analysis` walks what depends on the symbol, `depth` hops out, and
adds the files that mention it. It answers with counts and capped lists:
`direct`, `indirect`, `tests`, `public_api` (routes, controllers, handlers,
servers), `configuration`, and `files`, nearest first. `cross_project` lists
the nodes of other projects linked to the symbol, its file or a directory
above it, with evidence `link` - see
[Links between projects](#links-between-projects).

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
queue too. The bundled one starts with the stack once its weights are in
place:

```bash
make llm-model-install MODEL=nomic-embed
make up
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

## Links between projects

A link joins a node of one project to a node of another. Most are found by
the index run: a project takes a name from outside - an image in a compose
file, an Ansible role, a package in a manifest, a host it deploys to, an OS
package it installs, a bucket it uses - and
another indexed project provides that name. The run reads manifests and the edges it already wrote,
so links need no model and no embedding.

| Kind            | Taken from                                                 | Provided by                                                |
| --------------- | ---------------------------------------------------------- | ---------------------------------------------------------- |
| `image`         | `image:` of a compose service or of compose held in data   | a compose `build:` + `image:`, `docker build`/`tag`/`push` |
| `role`          | a role a play or a role applies                            | a `roles/<name>/` directory with `tasks/main.yml`          |
| `npm`           | `package.json` dependencies                                | `package.json` `name`                                      |
| `composer`      | `composer.json` `require`                                  | `composer.json` `name`                                     |
| `pypi`          | `pyproject.toml`, `setup.cfg`, `requirements.txt`          | `pyproject.toml` or `setup.cfg` name                       |
| `go`            | `go.mod` direct requirements                               | `go.mod` `module`                                          |
| `cargo`         | `Cargo.toml` dependency tables, following `package =`      | `Cargo.toml` `[package].name`                              |
| `cmake`         | `find_package()`                                           | `project()` in a `CMakeLists.txt`                          |
| `vcpkg`         | `vcpkg.json` dependencies                                  | `vcpkg.json` `name`                                        |
| `deploy-role`   | not taken across projects                                  | a workspace data file a layer key selects                  |
| `deploy-module` | `modules:` in workspace data, `requires:` of a module      | a workspace `modules/<name>/` directory                    |
| `host`          | a data file named like it, an inventory, a DNS record      | `instances:` beside a `.tf` file, a machine resource       |
| `tfmodule`      | a remote `source` of a Terraform module                    | declared by hand                                           |
| `package`       | a pyinfra module or Puppet class, a `.spec` `Requires:`    | a `.spec` `Name:` and its `%package` subpackages           |
| `bucket`        | a workspace `bucket:`, a Makefile variable named `*BUCKET` | a key of `buckets:` in a `config.yaml` beside a `.tf` file |

A deployment workspace is read the way hiera is, from its modules. Its
layout is what its data declares: a `hierarchy:` list such as
`nodes/{node}.yaml` names the layers below the directory holding it, and the
modules sit beside that directory in `modules/<name>/`.

A key named like a layer variable selects the file it fills (`role: web`
selects `roles/web.yaml`), `modules:` runs the modules it lists, a top-level
key naming a module configures it, and a data file named like a host that does
any of these deploys to that host. What a module installs or runs is read from
its [pyinfra](https://pyinfra.com) code: a `packages`, `files.download` or
`docker` operation, its arguments followed through the module's config to the
data, else to the defaults and properties of the config model.

A compose document held as a string in workspace or Puppet data takes the
images it names. A shell script or Makefile provides the image it builds,
retags or pushes under a name with a registry or namespace, at its build
context; variables of the file are expanded and a bare local name is not
provided.

An Ansible inventory, INI or YAML, found under its usual names, in an
`inventory/` directory or where an `ansible.cfg` points, uses the hosts it
lists. A Terraform machine resource provides the host its literal name is, and
a DNS record uses the hosts it names; only fully qualified names count.

Terraform, OpenTofu and Terragrunt files get edges too: a local module
`source` to the module's main file, `file()` and `templatefile()` to the file
they read, a Terragrunt `include` and `read_terragrunt_config` to the file
`find_in_parent_folders` would find, and a `dependency` to its unit. A path
built from a variable is skipped rather than guessed; `${path.module}`,
`${get_terragrunt_dir()}` and `${get_repo_root()}` resolve.

An image is matched without its tag and digest, a Python name is normalized as
pip does, a host is compared in lower case, and a module source loses its
getter, scheme and ref. A name two projects provide is not linked: a guess
would be wrong half the time, so it is reported instead. A name the taking
project provides itself is its own and links nowhere. Nothing under a test,
fixture, example or corpus directory (`tests/`, `fixtures/`, `examples/`,
`corpus/` and the like) provides a name, so a fixture naming a real package
cannot make it ambiguous; what such a tree takes is still taken.

What no file states is declared. An image a CI pipeline builds is an export
declared by hand, and every project taking it is linked from then on. A
relation such as `deploys_to` or `documents` is declared between two projects,
or two of their nodes:

```text
get_project_links(direction: "incoming", depth: 2)
save_project_export(kind: "image", name: "example.com/alpha/worker")
save_project_link(target_project: "beta", relation: "documents")
```

`get_project_links` answers with the projects reached and their distance, the
links between them rolled up by relation with sample node pairs, what the
project provides, and the names it takes that nothing or more than one project
provides. `describe_project` carries the same one-step summary, and
`get_code_graph_neighbors` lists the links of a node beside its edges. The
dashboard shows all of it on a project's Links tab, where declared relations
and exports are added and removed: a relation picks a node at each end, found
by searching that project's nodes.

A Puppet tree is read from its classes outward. A class takes every artifact
it runs or installs - an image, an OS package, a pip package - whether its
manifest names it in a `package`, `ensure_resource` or `create_resources`
call, an `image =>` attribute or hash key, or an RPM or pip artifact URL. A
variable is followed through the manifest, the class parameters with their
defaults, and whatever the tree's data sets under `class::param` or under a
key the class reads with `lookup` or `hiera*`, whatever the hiera layout.
Every YAML file naming the class under `classes:` applies it, a key it reads
configures it, and a file naming one of those by its stem under the key its
directory is named after - `role: web` beside `role/web.yaml` - selects it;
any of these named like a host deploys to that host. Names are compared by
their words, so `role: alpha::web` finds `role/alpha_web.yaml`,
`roles/alpha-web.yaml` or `roles/alpha/web.yaml`, whichever the tree uses; two
files in one directory spelling the same words are left unlinked. A role a
node is given by code, such as a regexp over its certname, is not followed. A Makefile running
`docker build -t` provides the image at its build context, and a
`.package.yaml` or `nfpm.yaml` the package it names.

`trace` follows one node across all of it:

```text
trace(project: "alpha", node_id: "tools/keeper/src/")
```

From the node it takes what a directory above it provides to the projects
taking it, climbs from each taker through whatever applies it - a role
including a class, a node selecting the role, a circuit using a module - and
records what those use in turn, such as the host a node deploys to and the
project creating it. Every step names the edge or the matched name behind it,
and the answer lists the chains from the node to where each one ends. The
Links tab runs it from a node picked there.

A question that starts from a name rather than from code - where is this host
described, what runs this image - goes to `find_linked_name`:

```text
find_linked_name(name: "web_01_example_com", project: "*")
```

It answers with every project and node providing the name and every one
taking it, whatever the case and separators: `web_01_example_com`,
`WEB-01.example.com` and the first label `web-01` all find the host
`web-01.example.com`, and an image is found by its last path segment too.
Both sides empty means no indexed project defines or uses the name in a form
the indexer reads; it may still be mentioned in text.

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

A suggestion can carry the question that failed and the code that should have
answered it, and then it is a test case as well as a complaint:

```text
save_suggestion(suggestion_id: "host-not-found", title: "...", detail: "...",
                query: "where is web-01 described",
                nodes: ["infra/hosts/web-01.yaml"])
get_suggestions(group_by: "directory")
```

`query` keeps up to twenty distinct questions, a new one added on each save.
`group_by` rolls the gaps up by `kind`, `lever`, `about` or the `directory` of
the nodes they name, each group with its count, its hits summed and the most
hit records; the dashboard shows the same table above the list. `make replay`
asks every recorded question of the running stack again and reports how often
`search_code` ranks a named node in its first five and ten results and whether
`get_context` reaches it, so a fix can be checked against what was actually
asked. It reads the fifty most hit suggestions and writes its results to
`eval/results/`.

## Plans

A plan is not derived from a tree, so it is not owned by a project: every
plan is a node of the built-in `_plans` project, keyed on a `plan_id` unique
across the database, with `project` a free-text tag in the node's metadata
rather than a key. Consequences worth knowing:

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

## Records about code

A memory, a plan or a suggestion can name the code it is about. `nodes` on
`save_memory`, `save_plan` and `save_suggestion` takes node ids in the project
the record is about, or `{ project, node_id }` for another one; each must
exist, a list given replaces the one stored, and a list left out keeps it:

```text
save_memory(memory_id: "no-orm", title: "The auth service avoids the ORM",
            text: "...", about: "alpha", nodes: ["src/auth/"])
get_memory(about: "alpha", node_id: "src/auth/jwt.ts")
```

A record about a directory is about everything below it, so the read above
finds the memory saved against `src/auth/`. The same `node_id` filter works on
`get_plans` and `get_suggestions`, and every record they return lists its
`nodes`, a node an index run has since dropped marked `missing` rather than
hidden. The reads that start from code bring the records back on their own:
`get_context` carries them under `knowledge` for its search hits,
`impact_analysis` for everything a change reaches, and
`get_code_graph_neighbors` lists them beside the edges. `describe_project`
counts them by type.

The links live in `record_links`. Dropping a memory, plan or suggestion
deletes its links in the same statement; an index run that drops a node leaves
the links to it, so the record shows the node as missing. Dropping a project removes the links to its code and keeps
the records; the drop report counts them. A single-project backup carries the
links to its code and restores those whose record still exists. On the
dashboard a record's page lists its nodes and adds or removes them, and a
node's panel lists the records about it under Knowledge.

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

- **Ask** - the opening page. Pick a session project and one of the read
  tools an agent has (`get_context`, `search_code`, the `find_*` family and
  the rest of the graph, project and record reads), fill in its arguments and
  run it. The dashboard calls the MCP server as a client of its own, so the
  tool descriptions, the argument schemas and the answer are the ones an
  agent gets; the page adds how long the call took and how many tokens the
  answer costs. Tools that write or drop are not offered.
- **Projects** - at `/projects`: what's indexed, where it came from, node/edge/file/plan
  counts, and how stale the index is. Three tabs: _Indexed_ for the trees,
  _Organizations_ for the projects that hold other projects, and _System_ for
  the built-in ones holding what an agent wrote. Searchable
  by name or path, sortable by every count and by freshness, with the type
  staged in place and applied only when the change is confirmed, and a `When`
  column naming the schedule each project resolves to. _New project_ registers one, with a host path or
  without: a project that reads no tree is an organization, which is what
  other projects are moved into.
- **A project** - five tabs: _overview_ (node type breakdown, the members it
  holds when it is an organization - each with its own settings, index and
  drop buttons, how it is kept indexed and how long ago it last ran - and the
  organizations holding it), _graph_ (the
  viewer's page, proxied so the frame shares this origin), _nodes_ (search
  and inspect one node's summary, metadata, neighbours and stored source),
  _files_ (file nodes with entity counts and hash status), _settings_ (when
  it is indexed, the formats its runs found, and what it ignores). Under the title: the sentence saying
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
  organization holding it has said otherwise: the indexing schedule. The
  ignore lines here apply to every project, under its own.

### Indexing on a schedule

A project is indexed by hand until something says otherwise, and what says
otherwise is a mode stored beside the ignore lines, at the same three levels:

| Mode       | What it does                                                  |
| ---------- | ------------------------------------------------------------- |
| `off`      | nothing runs on its own; the Index button is the only trigger |
| `periodic` | a run every N minutes                                         |
| `auto`     | a run when a file changes, throttled, and never otherwise     |

`auto` watches the mounted tree with inotify and starts a run once it has
been quiet for 15 seconds, every new change moving the start, and no sooner
than the throttle after the last run began, so a checkout or a `git rm` of
many files is indexed once it is over. A change made while a run is going
marks the project again and is indexed by the next run, including when that
start has to wait for the first one to end. Nothing else starts one: a tree
nothing changed is never walked again, and a watch that is blind - a network
filesystem, or `fs.inotify.max_user_watches` exhausted on the host - is
logged and retried, not covered by a full run. Such a project is indexed with the Index button.

Every field is resolved on its own, so a project may set `periodic` while its
interval is still the global one. The `When` column
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

At most `INDEX_MAX_RUNNING` runs go at once. A press past that limit is queued
and the button says `Queued...`; the queue is started oldest first as runs
end, and an organization queues the members past the limit. The queue lives in
the worker API's memory, so a restart drops it and the button is pressed again.

Registering a project writes a row and mounts nothing: the
compose override is a file on the host and both services hold the mounts they
started with, so `make mounts` there is what finishes the job. The dashboard
says so in its own reply rather than leaving it to be discovered.

A drop reports what it will cost before it happens and asks the project name
to be typed as confirmation. Dropping a directory is not a drop of what it
indexed - those nodes go on the next index run, the same way a deleted file's
do.
