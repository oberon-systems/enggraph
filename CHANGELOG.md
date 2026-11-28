## v3.1.0 (2026-11-28)

### Features

- **rust-tests**: Rust code compiled for tests only is a test wherever it sits
- **install-stack**: the stack installs, updates and goes away with one command and no checkout

### Bug Fixes

- **migrations**: revision 0011 brings the vector extension to the version of the image
- **codegraph**: a test holds that a Python import never reaches a file of another language
- **codegraph**: a Python call on a module name resolves in that module only
- **parsers**: a source file named dockerfile.go is read as Go, not as a Dockerfile
- **discovery**: the walk honours the tree's own .gitignore files and skips .terragrunt-cache
- **indexer**: a change to the linking code relinks a project's files on the next incremental run
- **neighbors**: get_code_graph_neighbors gives every relation its share and names what it cut
- **summaries**: a docstring is found past a long signature, a comment after the colon or a raw prefix
- **find-tests**: a sibling tests.rs is a test file for find_tests, get_context and search ranking
- **find-callees**: find_callees no longer claims a call into another file is not listed
- **install-stack**: the installer tests fail when the installer writes into its working directory
- **save-suggestion**: a correction saved without a bump keeps when the gap was last seen

### Build

- **compose**: nginx goes to 1.30.8-alpine
- **dependabot**: nginx stays on its stable line: no minor or major bumps to mainline
- **deps**: Bump the agent group across 1 directory with 2 updates
- **deps**: Bump pgvector/pgvector from pg16 to pg16
- **deps**: Bump https://github.com/astral-sh/ruff-pre-commit
- **deps**: Bump https://github.com/djlint/djLint
- **deps**: Bump https://github.com/rbubley/mirrors-prettier
- **deps**: Bump wyld-cz from 0.4.1 to 0.5.0 in the hooks group
- **deps**: Bump docker/login-action
- **deps**: Bump https://github.com/astral-sh/ruff-pre-commit
- **deps**: Bump https://github.com/djlint/djLint

### Documentation

- **readme**: the README ends with where enggraph is featured: the XERJ Corpus Hub
- **deployment**: the stack's one-command install is in the README and in full in the deployment page

## v3.0.0 (2026-11-16)

### Breaking Changes

- **trees**: make mounts, scripts/mounts.sh, INSTALL_MODE=mounts and the /code and /project mounts are removed, along with CODE_ROOT and SCAN_PATH. A project with no agent bound is no longer read or indexed. The worker API replies to POST /projects and to a rename no longer carry mounts, and its settings answer says readable instead of mounted. Before upgrading, install the agent on every host holding a tree, run enggraph-agent project import, and press Approve the import on the Agents page. Then delete the generated docker-compose.override.yaml, which make up refuses to start with, and run make build and make up. See docs/breaking-changes.md.
- **auth**: AUTH_ENFORCE defaults to on: an agent, script, remote worker or browser without a token is refused. Keep the old behaviour with AUTH_ENFORCE=off in .env, or move over in warn as docs/deployment.md Upgrading an existing install describes.

### Features

- **mcp**: every tool answer names where its text comes from
- **listcache**: count in the listing entries Valkey has lost
- **web**: the dashboard and the docs carry the enggraph logo and favicon
- **web**: a red lamp says an agent waits for approval
- **install**: make install onboards a tree through the host agent, compose untouched
- **mcp_register**: the address can name a stack on another host
- **agent**: project import offers what the stack reads from this host
- **agents**: an operator moves a host's projects onto its agent with one approval
- **agent**: a bound project is read through its host agent
- **agent**: a static host agent enrolls, holds a signed socket and announces its exports
- **agents**: a host agent enrolls, is approved by an operator and binds its exports
- **auth**: the gateway refuses a request without a token by default
- **auth**: every gateway request can name a person or a token, enforcement off by default
- **issues**: an issue carries a severity and a priority, both required to open one
- **issues**: a project's issues live in _issues, written by agents and drawn inside the project

### Bug Fixes

- **install-agent**: a second run over an installed agent asks nothing and changes nothing
- **search**: a text search answers within its own deadline, and the MCP server waits for it
- **jobs**: a summary job reads one window of the graph, not all of it
- **make**: images by digest, no secrets directory without OIDC, an eval stack with a password
- **gateway**: paths, hosts and the dashboard's own calls are judged as they are routed
- **mcp**: a call is weighed by who makes it, and does bounded work
- **core**: refusals, tokens and agent routes say and take no more than they must
- **scripts**: the installer and the backup take no name or address on trust
- **worker**: a secret stays on the machine and the weights are the pinned ones
- **agent**: a home, the state directory and a second name for a file are never served
- **gateway**: no answer may be framed by another origin or sniffed
- **workerapi**: an index refusal keeps its words only when they were written here
- **trees**: a fetched archive is weighed before it is unpacked
- **agent**: a swapped path reads nothing outside and a denied file is not even named
- **agent**: hold what the stack sends to what an agent can take
- **search**: a tree nobody could read is named, not answered as empty
- **agentauth**: a credential past its end still renews itself for a while
- **worker**: the GPU worker runs what it can check and takes no token as a flag
- **viewer**: the browser libraries are the pinned files and come from the stack alone
- **agents**: an approval is of the registration the operator looked at
- **trees**: an export no longer approved is never asked
- **viewer**: count a graph before loading it and draw few at once
- **web**: refuse what does not fit before the driver does
- **agenthub**: an agent's batches are counted over the whole answer
- **workerapi**: bound the lease, the job window, the search and every string
- **jobs**: a task record keeps its fields whatever a caller's words hold
- **dial**: a model server's reply is read up to a limit
- **errors**: an answer carries a code and an error id, the log the real text
- **auth**: weigh the path the services route on
- **mcp**: the open health check names no project
- **gateway**: answer the names in GATEWAY_HOSTS alone, on every route
- **auth-secret**: the OIDC files belong to the dashboard's user
- **install**: .env, the backups and a new settings file are their owner's alone
- **compose**: the services connect as a role without superuser
- **install**: allow the read tools alone and refuse plain http to another machine
- **agent**: refuse plain http to another machine and hold an import to --under
- **agentapi**: a signed request that writes is taken once
- **workerapi**: a second token opens the job routes alone
- **web**: a saved setting answers without its key and a probe takes http only
- **rauthy**: the bundled client asks for S256 and make auth-secret writes the secret alone
- **nginx**: the login redirect carries the request URI as nginx received it
- **auth**: hold the worker API and the operator role to who asks
- **mcp**: bound what one tool call may read, carry and run
- **mcp**: weigh every project a call names against the token
- **embedloop**: read owed summaries on marks and continue from the last id
- **summaries**: mark a project wherever its summaries are written
- **queue**: mark the projects whose summaries changed
- **listcache**: keep the projects listing counts in Valkey and drop diskcache
- **pages**: docs deploy only when the docs change, and merges in a row queue instead of failing
- **install**: make install runs on a checkout without .env
- **nginx**: the login redirect is relative, so it keeps the port the browser used
- **web**: the login page no longer polls the lamps, which reloaded it forever
- **agent**: a credential refresh keeps the watch, so it owes no resync
- **pre-commit**: the cargo hooks run once per commit
- **indexer**: an index run rewrites only what moved, and a parser change rereads only its own files
- **web**: a project the listing cache has not seen sorts its node counts as numbers

### Refactor

- **test_codegraph**: wrap the multi-line lambdas the way ruff 0.17 formats them
- **trees**: trees are read through host agents, the mounts are gone

### Build

- **agent**: the crate names its repository
- **pre-commit**: markdownlint runs on a Node of its own, not on whatever the host has
- **ci**: llama.cpp is bumped by its own workflow once a build is 14 days old
- **deps**: Bump valkey/valkey from 8.1-alpine to 9.1-alpine
- **deps**: Bump actions/upload-artifact from 4.6.2 to 7.0.2
- **deps**: Bump docker/build-push-action from 5.4.0 to 7.4.0
- **deps**: Bump actions/configure-pages from 5.0.0 to 6.0.0
- **deps**: Bump actions/upload-pages-artifact from 3.0.1 to 5.0.0
- **deps**: Bump https://github.com/commitizen-tools/commitizen
- **deps**: Bump https://github.com/djlint/djLint
- **deps**: Bump https://github.com/astral-sh/ruff-pre-commit
- **deps**: Bump https://github.com/igorshubovych/markdownlint-cli
- **deps**: Bump https://github.com/rbubley/mirrors-prettier
- **deps**: Bump docker/login-action from 3.7.0 to 4.7.0
- **ci**: hooks, their images and a release tag are pinned to what was reviewed
- **workflows**: every action is named by its commit
- **images**: no installer stays in a service image
- **workflows**: the test and pages jobs hold the token scope they use
- **release**: images are published from a release tag alone, with a compose file naming them by digest
- **deps**: install every package by hash and name every image by digest
- **pre-commit**: gitleaks reads every commit and every file make lint names
- **compose**: drop the list-cache volume of the worker API
- **deps**: Bump the python group across 6 directories with 19 updates
- **deps**: Bump starlette from 0.49.3 to 1.3.1 in /packages/mcp
- **deps**: Bump the agent group in /packages/agent with 3 updates
- **deps**: Bump the compose group with 2 updates (#4)
- **deps**: Bump https://github.com/hadolint/hadolint
- **deps**: Bump https://github.com/pre-commit/pre-commit-hooks
- **deps**: Bump https://github.com/astral-sh/ruff-pre-commit
- **deps**: Bump https://github.com/koalaman/shellcheck-precommit
- **deps**: Bump https://github.com/djlint/djLint
- **deps**: Bump actions/checkout from 4 to 7
- **deps**: Bump actions/setup-python from 5 to 7
- **deps**: Bump actions/deploy-pages from 4 to 5
- **deps**: Bump docker/setup-buildx-action from 3 to 4
- **deps**: Bump rust-lang/crates-io-auth-action
- **deps**: Bump pytest from 8.3.4 to 9.0.3
- **deps**: Bump joserfc from 1.7.5 to 1.7.6 in /packages/web
- **deps**: Bump authlib from 1.8.0 to 1.9.1 in /packages/web
- **deps**: dependabot watches every dependency, two weeks behind unless a security fix
- **agent**: the agent is released on its own, from cz bump in packages/agent

### Documentation

- **readme**: the README has a table of contents and says how to install, update and remove the host agent
- **deployment**: the token rules of the worker API, operators by login and the pinned artifacts
- **deployment**: the listing counts are kept in Valkey
- **security**: the security policy says what is supported, how to report and what is in scope
- **agent**: installing, updating and moving projects onto the host agent

## v2.0.0 (2026-10-20)

### Breaking Changes

- **indexer**: The compose service graphify is now indexer (indexer-beta in the eval stack) and GRAPHIFY_CPUS, GRAPHIFY_MEM and GRAPHIFY_MEM_RESERVATION are INDEXER_CPUS, INDEXER_MEM and INDEXER_MEM_RESERVATION. The graph MCP server and graph.json are removed. Run make up, which rewrites the generated override, rename the settings in .env and remove the old graph-out volume; docs/breaking-changes.md has the steps.

### Features

- **make**: make test runs only the suites a change reaches
- **indexer**: the upstream extractor is removed, the index job is the indexer service
- **indexer**: Scala is extracted by our own parser
- **indexer**: C++ is extracted by our own parser
- **indexer**: C is extracted by our own parser
- **indexer**: Ruby is extracted by our own parser
- **indexer**: C# is extracted by our own parser
- **indexer**: Kotlin is extracted by our own parser
- **indexer**: Java is extracted by our own parser
- **indexer**: Rust is extracted by our own parser
- **indexer**: PHP is extracted by our own parser
- **mcp**: get_plans lists titles and reads one plan in full by id
- **indexer**: Go is extracted by our own parser
- **indexer**: JavaScript is extracted by our own parser
- **indexer**: TypeScript is extracted by our own parser
- **indexer**: Python is extracted by our own parser, in two passes
- **indexer**: RPM specs, pip requirements, setup.cfg and systemd units are parsed
- **mcp**: an identifier in a query is matched whole by search_code
- **plans**: prompts and roadmaps are tables of their own, drawn inside the project
- **mcp**: search_code takes a mode: lexical, vector or hybrid

### Bug Fixes

- **indexer**: C++ gtest bodies are named by their suite and test
- **indexer**: C++ calls by name reach only the headers a file includes itself
- **indexer**: C++ calls on values of no known type no longer reach unrelated types
- **indexer**: Ruby requires bind only through the load path
- **mcp**: the calls note names every language with cross-file edges
- **stats**: the gauges are recounted only for the projects that changed
- **web**: the project page reads its counts from the listing cache
- **storage**: an edge our extractor redraws is no longer cleared with graphifyy's
- **web**: a moment read back as a datetime is spelled, so the prompts tab renders
- **indexer**: a fixture tree takes nothing from other projects
- **mcp**: a sentence is not compared with node names as a whole
- **mcp**: node summaries are searched through a table of their words
- **mcp**: the lexical lists of a search read only the projects in scope

### Build

- **docker**: pip wheels are kept in a build cache across image builds
- **make**: a test run installs the packages first when a manifest is newer

### Documentation

- **roadmap**: ROADMAP.md is removed, the roadmap is the one the MCP server holds

## v1.0.0 (2026-10-09)

### Breaking Changes

- **make**: make jobs and make job are removed. A summary job is opened by the summarize service or by a remote worker, and the Queues page of the dashboard shows how far along it is.
- **web**: Node.js is no longer used to build, run or develop any part of the stack. The web directory, make web <target> and scripts/web-check.sh are removed: the dashboard is built by make build with the other images, so run make init, then make build or make pull, before make up. An address no page lives at answers 404, where it answered 200.
- **mcp-server**: The image ghcr.io/oberon-systems/enggraph/mcp-server is no longer built: the compose service keeps the name mcp-server and runs the mcp image, so run make build or make pull before make up. The mcp-server directory, make mcp <target> and make test-mcp are removed; the server's tests run with make test-py.
- **migrate**: A database below goose migration 31 is refused and no service starts: upgrade it with 0.24.0, the last release that carries goose, run make up there, then upgrade to this one. A database at 31 is marked as revision 0001 on the first make up and is not changed. The revisions are baked into the api image, so a new one is applied only after make build. The migrations/ directory no longer exists.

### Features

- **web**: the dashboard is a Python package, answering its API as the Node one did
- **mcp**: the MCP server is a Python package, answering as the TypeScript one did
- **core**: the schema is an Alembic revision, and the tables are SQLModel classes

### Bug Fixes

- **eval**: make parity walks a fresh stack twice before it compares

### Refactor

- **core**: the migrate run knows a 0.24.0 schema by its tables, and goose is gone from the code
- **core**: the statement runner and the JSON writer move from the MCP server into core

### Build

- **cz**: the packages carry the release version, and cz bump keeps it there
- **make**: make jobs and make job are removed
- **web**: the web image is built from packages/web, and the Node dashboard is gone
- **pre-commit**: djlint reads the dashboard's templates, and the eslint and tsc hook is gone
- **mcp-server**: the mcp-server service runs the mcp image, and the TypeScript server is gone
- **migrate**: the migrate service applies Alembic revisions from the api image, and goose is gone

### Documentation

- **upgrade**: BREAKING CHANGES: the path from 0.x to 1.0.0 goes through 0.24.0
- **web**: BREAKING CHANGES: the dashboard is a Python package and Node.js is not needed
- **mcp**: BREAKING CHANGES: the MCP server is a Python package and its image is mcp
- **migrate**: BREAKING CHANGES: Alembic owns the schema, and a database below goose 31 is refused

## v0.24.0 (2026-10-07)

### Breaking Changes

- **compose**: The graphify image is gone: pull or build api, embed, summarize and viewer instead. make summarize, make embed and make graphify <target> are removed, since the queues drain on their own. The embedding and summary loops no longer run inside worker-api, so a stack started without the embed and summarize services drains neither queue. Run make up once after upgrading: it rewrites docker-compose.override.yaml so that both services read /code.
- **packages**: The built-in GGUF summarizer and llama-cpp-python are removed, with the SUMMARIZE, LLM_MODEL_DIR, LLM_MODEL_PATH, LLM_THREADS, LLM_CTX and SUMMARY_LIMIT settings: summaries come from a llama.cpp server or a remote worker. The embed.py command line is removed: the embedding queue is the only way a chunk gets its vector. Modules are imported as enggraph.<package>.<module>, not enggraph.<module>.
- **database**: Tables are renamed: graph_nodes -> nodes, graph_edges -> edges, code_embeddings -> chunks (empty, refilled by the embedding queue), file_hashes -> indexed_files, project_settings -> settings, and seven more; no foreign keys remain. Single-project .sql.gz backups taken before 0031 do not restore. A project is dropped through the worker API only. Migrate: take a whole-database backup, stop the services, make build and make up. A database whose catalog is damaged or that holds rows of dropped projects is restored into a fresh database first. See docs/breaking-changes.md.

### Features

- **restore**: a restore shows its progress and builds indexes in one process
- **database**: no table holds a foreign key, and file text leaves the database
- **skills**: a database skill every onboarded codebase gets by default
- **scheduler**: an auto run waits until the watched tree has been quiet
- **backup**: a single-project backup carries the record links to its code
- **web**: a record's page lists and edits the code it is about, and a node's panel lists what was written about it
- **mcp-server**: memories, plans and suggestions name the code they are about and come back from it
- **migrations**: record_nodes ties a memory, plan or suggestion to the code nodes it is about
- **mcp-server**: find_linked_name answers who provides and who takes a host, image or package name across projects

### Bug Fixes

- **mcp-server**: make replay waits for a search across every project
- **graphify**: a fixture provides no name, and a docker build option value is not its context
- **graphify**: a data value selects its file by its words, whatever separates them in a given tree
- **graphify**: a file keyed by its own name provides a host only when the name is a host name
- **mcp-server**: trace climbs from a file through the classes it defines

### Refactor

- **packages**: the Python code is six packages under packages/, and graphify/ is gone

### Build

- **compose**: an image per package, and the two queues run as services of their own
- **commitizen**: wyld-cz 0.4.1 is required, with its breaking change question

### Documentation

- **packages**: BREAKING CHANGES: the packages layout, the four images and the queue services
- **breaking-changes**: BREAKING CHANGES: dropping a project broke the database up to 0030
- **deployment**: every upgrade starts with a whole-database backup
- **usage**: records about code, the suggestion feedback loop and make replay are described
- **skills**: the commit skill uses commitizen only where the repository has it, and otherwise follows the repository's own history

## v0.23.0 (2026-10-05)

### Features

- **graphify**: an index run past the limit is queued in memory instead of refused
- **graphify**: a deployment workspace is read like hiera from its module code, and shell images, inventories and Terraform hosts are linked
- **graphify**: a Puppet data file named like a host deploys to it whenever it applies, configures or selects
- **graphify**: a Puppet value is followed across classes and data, and package hashes, ensure_resource and artifact URLs are read
- **graphify**: a host is provided by any YAML beside a circuit and by a file keyed by its own host name
- **web**: the Links tab traces a node picked there across projects
- **mcp-server**: trace follows one node across projects, from what provides it to the host it is deployed on
- **graphify**: a Puppet class is followed into its data, and what it runs is matched to what another project builds
- **web**: the export kinds list package and bucket
- **mcp-server**: package and bucket are link kinds
- **graphify**: OS packages and buckets are link kinds
- **web**: a relation is declared node to node, either end on this project, and an export names its node by search
- **mcp-server**: deploy-role, deploy-module, host and tfmodule are link kinds
- **graphify**: a deployment workspace, Terraform sources and the hosts a circuit creates become edges and links
- **backup**: a single-project backup carries what the project provides, takes and declares
- **web**: a Links tab shows which projects use this one and what it provides, and declares the rest
- **mcp-server**: links between projects are read and declared through five tools, and impact_analysis fills cross_project
- **graphify**: an index run records what a tree provides to other projects and what it takes
- **migrations**: what each project provides and takes is stored, and a view links the projects
- **web**: an Ask page opens the dashboard and calls the read tools of the MCP server as an agent does
- **mcp-server**: the first tool call of a session carries the skill check
- **skills**: skills are served from the database with a sha256 version, and agents update stale copies
- **selection**: every supported format is indexed, scripts by their shebang, and ignore lines are summed across levels

### Bug Fixes

- **pre-commit**: tofu fmt runs in the OpenTofu image, not from a tofu on PATH
- **web**: an organization gets a read-only Links tab, and the first call of a session parses
- **mcp-server**: get_project_links on an organization keeps the links between its members
- **selection**: secrets are never indexed, and the global ignore default is the whole list
- **index**: auto indexes a project only when the watch reports a change

### Build

- **eval**: the beta workspace declares every layer of its hierarchy
- **eval**: alpha creates the bucket beta installs packages from
- **eval**: alpha creates a host and beta's workspace deploys to it
- **pre-commit**: tofu fmt checks every .tf and .tfvars file
- **eval**: beta is indexed beside alpha and takes its image and both of its packages
- **make**: skill-install and its siblings are gone, the MCP server hands out the skills

### Documentation

- **usage**: the hiera-like workspace, shell and Makefile images, inventories and Terraform hosts are described
- **skills**: the enggraph skill sends where-does-this-code-end-up questions to trace
- **usage**: trace and the Puppet, Makefile and package marker rules are described
- **usage**: the package and bucket link kinds are described
- **skills**: the enggraph skill asks get_project_links which repository creates the hosts a deployment reaches
- **usage**: workspace edges, Terraform relations and the host and module link kinds are described
- **skills**: the enggraph skill sends a question across repositories to get_project_links
- **usage**: links between projects are described, and Cross-Project Lookups is done
- **roadmap**: cross-file calls and shebang support are marked done

## v0.22.0 (2026-09-25)

### Features

- **index**: one index run at a time, and the projects list reads its counts from a diskcache

### Build

- **make**: init installs the indexer dependencies, and leaving make shell is not an error
- **compose**: the embedder is part of the stack, and EMBED=1 is gone

## v0.21.0 (2026-09-24)

### Features

- **queue**: a summary job holds a window of its project and tops it up from the graph
- **queue**: the work queues move from postgres to valkey and the dashboard reads gauges
- **context**: imports and importers decay with the rank of the hit they hang off
- **indexer**: a call or an import reaches the file across the tree, not only within one
- **web**: the queues page shows directories, symbols and summary vectors
- **search**: a broad question climbs the summary ladder and get_overview drills down it
- **summaries**: every directory and symbol is summarized and embedded like a file

### Bug Fixes

- **queue**: a job holding everything owed stops rescanning the graph before every claim

### Build

- **compose**: the whole stack shares one CPU and memory budget, and valkey joins it
- **eval**: the report counts packet noise by tier and the baseline takes the cross-file graph
- **eval**: overview questions are scored and the baseline has a target of its own

### Documentation

- **queue**: resource limits and the queues in valkey are described
- **summarization**: summaries at every level, and how to read them

## v0.20.0 (2026-09-23)

### Features

- **search**: a chunk is scored by how rare the words it matches are
- **search**: camelCase is split in the lexical index and the query alike
- **search**: question words are matched as light stem prefixes
- **embed**: chunks are cut on entity starts and a new cut re-embeds every file
- **rerank**: vendored and test paths sink below the code they describe
- **search**: the lexical half matches any content word of a question
- **mcp**: symbol tools and impact_analysis navigate the graph by name
- **eval**: a benchmark and a throwaway stack check retrieval, SQL and every tool
- **mcp**: get_context assembles the context for a question in one call

### Bug Fixes

- **interop**: two files of one stem no longer share their edges
- **identifiers**: a tree recreated behind its mount is refused, not read as empty

### Build

- **eval**: make test scores the benchmark and long questions join it
- **make**: every test suite has a target and make test runs them all
- **dev/web**: the dashboard mockups are drawn in a penpot-local-stack

### Documentation

- **embedding**: the lexical half describes stems, camelCase and IDF
- **embedding**: search and chunking describe the OR lexical half and the new cut
- **base**: the code of conduct lists items the way the linter wants
- **base**: the context engine is written down where it is looked for

## v0.19.0 (2026-09-17)

### Features

- **mcp**: search_code reranks the fused candidates before it answers
- **graphify**: each queue's server says whether it answered, per project

### Bug Fixes

- **web**: a plan can be moved to another project, once confirmed
- **web**: a plan can be made for any project, from a list that scrolls
- **base**: make -C install onboards the directory make was called from
- **web**: settings keep what was typed, and lamps show whether a queue answers
- **base**: no real names in tests, docs or examples
- **base**: a failed enqueue or a spent task no longer wedges a queue
- **base**: queues that drain per server, skip what fails and say so

## v0.18.0 (2026-09-10)

### Features

- **base**: the vector half of search, its queue, and the switches that gate it

### Bug Fixes

- **base**: search_code parses: BOTH is a reserved word and cannot name a subquery
- **base**: coverage counts the files a queue can actually take
- **base**: the embedding queue is filled from the graph, not from the file hashes

## v0.17.0 (2026-09-09)

### Features

- **base**: a project is one tree, and an organization is how projects group
- **base**: a project carries a sentence saying what it is for
- **web**: a project moved into an organization leaves the projects list, and a whole tree has no directories to settle
- **base**: a project moved into an organization is listed there instead
- **base**: a project is added to an organization or moved into one
- **web**: settings, index and fresh on every directory and every member
- **base**: index one directory of a project on its own
- **web**: each board shows only what its kind of project answers
- **web**: count the members of a project, and hold an organization to its type
- **base**: a member reads what its organization sets
- **mcp**: searching an organization reaches every project it holds
- **web**: move, detach and organization members on the project page
- **web**: routes for moving directories and holding projects
- **base**: organizations, and directories that move between projects

### Bug Fixes

- **base**: multiple fixes
- **web**: an error is written out under the control, and moving into an organization is on the project page
- **base**: an organization holds projects, and reads no directory of its own
- **web**: a failed run is readable, and every directory says when it indexes
- **base**: every project of named directories indexes again, one slice or all
- **web**: a settings page settles one level and names where the others are
- **web**: centre the board page, heading and tabs included
- **web**: centre the projects list and nothing else
- **web**: every control that changes something says so and asks first
- **web**: say what an organization holds, and centre a board that is not wide

### Refactor

- **base**: the project is enggraph, and so is everything it writes

### Documentation

- **onboarding**: an organization is a settings level its members inherit
- **onboarding**: which project a directory belongs to, and what an organization is

## v0.16.0 (2026-09-06)

### Features

- **web**: say on the projects list when each project indexes itself
- **graphify**: fold the schedule of every project in one endpoint

### Documentation

- **usage**: name the column that says when a project indexes itself

## v0.15.0 (2026-09-03)

### Features

- **web**: set the indexing schedule at every level it resolves from
- **graphify**: index a project on a schedule, off, periodic or auto
- **web**: edit what a project indexes, and the directories it reads
- **web**: register a project, change its type, and find one in the list
- **scripts, make**: onboard a codebase without writing selection files into it
- **workerapi**: register a project, scan a directory, say where a selection came from
- **migrations, graphify**: hold the selection of a project in the database
- **scripts, make, docs**: grant claude the standing permission gemini gets from trust
- **workerapi, web, mcp-server**: show what a project reads
- **graphify, scripts, make**: mount one directory per source
- **graphify**: index a project from several directories
- **migrations**: hold the directories a project reads

### Bug Fixes

- **graphify**: run the suite under the console script as well
- **scripts**: carry a project's selection through a backup
- **scripts**: carry the project sources through a backup

### Build

- **context**: remove ctx files, moved into db

### Documentation

- **readme, docs, roadmap**: when a project indexes itself, and what goes blind about it
- **readme, docs, roadmap**: the selection is a setting, not a file in the tree
- **readme**: give the registration section runbook form
- **readme**: name the three scopes a registration can be kept in
- **templates**: state the commit and branch rules
- **readme, docs, roadmap**: a monorepo indexed in slices

## v0.14.2 (2026-08-26)

### Refactor

- **make, scripts, graphify**: register the project row when its mount is written

## v0.14.1 (2026-08-26)

### Bug Fixes

- **graphify**: report compose files and drop volatile counts
- **install, templates**: keep a codebase's own GEMINI.md

### Build

- **ctxkeep**: regenerate the selection, keeping *.conf by hand

### Documentation

- **roadmap**: settings and formats move to the database

## v0.14.0 (2026-08-26)

### Features

- **make, scripts**: install FORCE=1 offers to refresh what a project already has
- **web**: index a project from the dashboard
- **graphify, migrations**: indexing runs in the API instead of a container
- **graphify, web, migrations**: file text leaves the database for the mount
- **graphify, compose, make**: mount every indexed tree at /code/<project>
- **graphify**: html support added
- **migrations, mcp-server, web**: plans move to the _plans built-in project
- **graphify**: update docker compose parsing
- **worker**: auto mode added
- **ai**: update cloud's local settings
- **agents**: cloud local template updated

### Bug Fixes

- **mcp-server, web**: GATEWAY_HOSTS=* reaches the MCP server too
- **scripts**: the nginx config check pins the image compose runs
- **graphify**: set fingerprints for files hashes
- **web/server**: guard: fix error message
- **compose**: up nginx version to 1.30.4

### Refactor

- **make, scripts, docs**: one install target and one alias

### Documentation

- **docs**: roadmap gains per-project file tracking
- **docs, instructions**: updated

## v0.13.0 (2026-08-21)

### Features

- **web**: single entry point
- **make**: add skill-reinstall, and the two skill aliases it needs
- **skills**: install the context and write-docs skills, drop graphify
- **worker**: start the model server from a batch file, not a typed command line
- **worker**: run the model on a llama.cpp server, not only in this process
- **worker**: fetch llama-server for whatever driver the machine turns out to have

### Documentation

- **summarization.md**: say what it does, then the three ways to run it, then which to pick
- **worker**: a runbook for the model server, and what 0xc000001d means
- **worker**: make the GPU setup one copy-paste block that ends in a running worker

## v0.12.0 (2026-08-20)

### Features

- **make**: install every skill under skills/, not only graphify
- **core**: implement _suggestions support
- **core**: implement repository categorization and agents memory support
- **workerapi**: summarize a project from a machine with a GPU
- **graphify**: pick the summarizer model with MODEL=, defaulting to Qwen2.5-Coder

### Bug Fixes

- **worker**: load the model before the job and say what llama.dll is missing
- **pages**: build the site from docs/Gemfile instead of the github-pages gems
- **worker**: pin llama-cpp-python to a version that actually has wheels

### Build

- **github**: added action for deploy github pages

### Documentation

- **ROADMAP.md**: record a reinstall target for already onboarded tools
- **CLAUDE.md**: shrink the always-on agent instruction files
- **docs**: restructure Pages docs and trim README to point at them
- **pages**: fix site config
- **pages**: added docks for github pages

## v0.11.0 (2026-08-20)

### Features

- **graphify**: summarize file nodes with a local model in its own pass
- **make**: implement context reindex command
- **web**: show the projects, the graph and the plans
- **web**: read and write the plans of every project
- **web**: browse the nodes, their neighbours and the files
- **web**: serve the projects and the viewer's graph
- **web**: add the dashboard service, empty but wired
- **index.ts**: let the plan tools read and write the type
- **migrations**: add a type column to plans, apart from status
- **plans**: implement cross-project plans
- **make**: unified onboarding added
- **database**: implement schema management
- **make**: implement support for local builds
- **Makefile**: keep the seven newest backups by default
- **scripts**: back up and restore the graph database
- **index**: re-index must invalidate the extractor cach
- **tools**: drop a project from the graph
- **compose**: use latest tag for applicaiton images
- **database**: move the database out of the working tree
- **PuppetParser**: resolve qualified names into reads_var and references edges
- **JsonParser**: index .json and read what its manifests declare

### Bug Fixes

- **config.py**: index .tsx, which arrived with the dashboard
- **Makefile**: build and run one image reference, tagged latest
- **database**: DATA_DIR removed completly
- **MakeParser**: read the whole Makefile and keep only its real targets
- **indexer**: link every entity to the file that declares it

### Build

- **github**: update build triggers and cache using
- **Makefile**: install the commit adapter best-effort
- **github**: actions: added tag latest
- **github**: action for build images added
- **gemini**: update instructions
- **CLAUDE.local.md**: write down the commitizen question flow and how to drive it
- **CLAUDE.local.md**: record template plans, the suggestions recap line and the gemini trust flag
- **.ctxkeep**: index this repository through an explicit keep list
- **CLAUDE.local.md**: require a recap of how the answer was produced

### Documentation

- **README.md**: document the dashboard and the plan type
- **README.md**: drop every trace of DATA_DIR and COMPOSE_PROJECT_NAME
- **CLAUDE.md**: drop the puppet residue and stop assuming one cz adapter
- **roadmap**: refatored and updated
- **ROADMAP.md**: rank the deferred work by cost and record the gaps found since
- **01-init.sql**: describe both metadata keys the indexer writes
- **ROADMAP.md**: drop the finished Make parser item and renumber

## v0.10.1 (2026-08-17)

### Bug Fixes

- **compose**: pin the stack to a single compose project again

### Build

- **CLAUDE.local.md**: forbid reading anything the loaded plan does not name
- **CLAUDE.local.md**: add the local agent instructions

### Documentation

- **ROADMAP.md**: drop the completed items and renumber what is left

## v0.10.0 (2026-08-17)

### Features

- **graphify**: index Puppet manifests and their templates
- **graphify**: index C and C++ sources

### Build

- **CLAUDE.md**: added instractions for delegating tasks to gemini light

### Documentation

- **ROADMAP.md**: added task for add cross-projects relations and queries

## v0.9.1 (2026-08-17)

### Bug Fixes

- **mcp**: bind both clients to the per-project endpoint

### Documentation

- **roadmap**: record the work the neighbouring projects exposed

## v0.9.0 (2026-08-16)

### Features

- **multi-project**: hold every indexed codebase in one database

### Bug Fixes

- **compose**: put the network comment above the block it explains
- **skills**: install the skill into the root the agent actually reads
- **compose**: name the compose project after the indexed codebase

### Documentation

- **roadmap**: record moving the database out of the working tree

## v0.8.0 (2026-08-16)

### Features

- **skills**: register the graphify skill for both agents
- **graphify**: extract code with graphifyy, keep the database
- **mcp-server**: serve the graph over streamable http
- **mcp-server**: added tools for interact with file hashes

### Documentation

- **project**: describe the two producer pipeline

## v0.7.0 (2026-08-15)

### Features

- **graphify**: implement incremental indexing

## v0.6.0 (2026-08-15)

### Refactor

- **graphify**: split the indexer into a package under src

### Documentation

- **graphify**: point the docs at the package layout

## v0.5.3 (2026-08-15)

### Features

- **graphify**: read Ansible YAML as Ansible

### Bug Fixes

- **mcp-server**: return the summary the graph already stores
- **graphify**: summarize a file by its title or what it declares

### Documentation

- **README**: document the Ansible edges

## v0.5.2 (2026-08-15)

### Bug Fixes

- **mcp-server**: keep a saved summary from being overwritten by the indexer
- **graphify**: port the parsers to the tree-sitter 0.26 query API

### Documentation

- **ROADMAP**: record what the tree-sitter step delivers and what incremental indexing still owes
- **README**: describe what the indexer actually selects and summarizes

## v0.5.1 (2026-08-15)

### Bug Fixes

- **graphify**: implement missing abstract method get_relations in YAMLParser

## v0.5.0 (2026-08-15)

### Features

- **graphify**: implement full Tree-sitter AST parsing and import resolution

### Documentation

- **README**: add documentation for summaries and planning
- **ROADMAP**: mark Tree-sitter implementation as completed

## v0.4.0 (2026-08-15)

### Features

- **graphify**: implement automatic summaries and persistent project planning

### Documentation

- **GEMINI**: add project instructions

## v0.3.1 (2026-08-15)

### Bug Fixes

- **make**: say none running when the stack is down

## v0.3.0 (2026-08-15)

### Features

- **make**: report stack and index status

### Documentation

- **ROADMAP.md**: extend reodmap for 7 planned points

## v0.2.1 (2026-08-15)

### Bug Fixes

- **make**: delete the database that clean promised to delete

### Documentation

- **readme**: correct what clean removes

## v0.2.0 (2026-08-15)

### Features

- **graphify**: let the project choose what gets indexed

### Build

- **gitignore**: ignore the jetbrains project directory

### Documentation

- **readme**: document the indexing configuration files

## v0.1.0 (2026-08-14)

### Features

- **stack**: add graphrag context service with tooling and docs
