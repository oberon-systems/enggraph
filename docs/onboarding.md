---
layout: default
title: Onboarding
nav_order: 2
---

## Prerequisites

- Docker with the Compose plugin.
- Python 3.11+ (for internal tools).
- Node.js 20+ (for MCP tools).

## Quick start

1. Initialize the environment:

   ```bash
   make init
   ```

2. Configure your environment: Edit the generated `.env` file to set `PROJECT_PATH` (the path to your codebase) and `POSTGRES_PASSWORD`.

3. Build the services:

   ```bash
   make build
   ```

4. Start the stack:

   ```bash
   make up
   ```

5. Onboard your codebase:

   ```bash
   make install AGENT_ROOT=/path/to/your/project
   ```

   This registers the project and mounts its tree; it does not build the
   graph. The dashboard lists it as `never indexed` with an Index button
   beside it, and that button is what indexes it.

## Adding new codebases

You can index multiple codebases into the same database stack:

```bash
context-install                 # from /path/to/another/project
context-install TYPE=docs       # from /path/to/a/handbook
```

The stack manages them by path, and you can access them by name via MCP.
`TYPE=` categorises a project - `codebase` (the default), `docs` or `config` -
which is what `search_code_nodes` narrows on when it searches every project at
once. It is stored with the row, so a later run without `TYPE=` keeps it.
Names beginning with `_` are refused: they belong to the built-in projects,
`_memory` today.

Onboarding a tree twice is safe. No file that exists is replaced, and the
project row keeps the type and the index date it already had.

## A monorepo, in slices

A project can read several directories instead of one tree. Each is mounted
read-only at `/code/<project>/<alias>` and walked into the same graph, so the
pieces of a monorepo that matter are indexed without the rest of it.

Register the project first, then hand it one directory at a time:

```bash
cd /home/you/work/mono
context-project PROJECT_NAME=mono
cd deploy/configs
context-source mono
cd ../../tools/agents
context-source mono agents
```

`context-project` is `make install SOURCE=none`: it writes the agent files,
the skills and the project row, and registers no directory. `context-source`
adds the directory you stand in, under an alias taken from its name or given
as the second argument.

The alias becomes the first segment of every node id that directory produced,
so `deploy/configs/prod/nginx.conf` is `configs/prod/nginx.conf` in the graph.
Two slices may hold a file of the same name without colliding, and one
extraction pass still resolves a call from one slice into the other.

Each directory carries its own selection, resolved from its own settings row
rather than from the repository the slices were cut from. The settings tab of
the project's page edits one per directory; see
[formats](https://oberon-systems.github.io/claude-context-mcp/formats.html).

## Changing what a project reads

```bash
context-sources                              # every project, every directory
context-sources PROJECT_NAME=mono            # one of them
context-source-drop mono configs             # stop reading one
make source-promote PROJECT_NAME=api ALIAS=root
```

Each of these rewrites `docker-compose.override.yaml` and recreates the API,
because a running service holds the mounts it was started with.

Dropping a directory leaves its nodes in the graph until the next index run
prunes them, the same path a deleted file takes.

`source-promote` names the single unnamed directory of a project indexed
whole, which is what lets a second one join it. Every node id gains the alias
as its first segment, so index the project again afterwards.

## Moving directories between projects

Which project a directory belongs to is not settled once. A tree onboarded on
its own turns out to be part of a bigger one, a slice belongs under a different
umbrella, or one has to come back out and stand alone again. All three are
dashboard-only: the shell aliases and the make targets change what one project
reads, never which project a directory belongs to.

All three are on the overview tab, under _Directories_:

- _Move a project in_ folds a whole project into this one. Its directories
  become directories of this project, under an alias each, and the project they
  came from is dropped.
- _Move_, on one directory's row, sends that directory to another project.
  Both projects survive.
- _Detach_, on the same row, takes the directory out into a project of its own,
  mounted whole. This is the inverse of the merge.

What each of them costs is the same in every direction:

- The mount is a file on the host. Run `make mounts` there and recreate the API
  before either project can be indexed.
- The graph does not travel. A node id carries the alias of the directory it
  came from as its first segment, and nothing rewrites ids, so index both
  projects again. Summaries written by hand are lost with the ids they were
  written against.
- What the old project built from that directory is deleted straight away
  rather than waiting for a prune. A project left reading nothing is never
  indexed again, so a prune there would never run.

They differ on what happens to the name a directory leaves behind. Merging
drops the project, and moving a project's _only_ directory is that project
moving, so it is dropped too. In both cases the plans, memories and
suggestions written about that name follow the directories, because the name
they describe is about to stop existing. Moving one directory out of several
drops nothing, and neither does detaching: a container that hands a slice back
is meant to take another, so it stays, reading nothing until it does.

A project mounted whole takes no directory in, its own included: name its root
with `source-promote` first, then move.

## Container projects

A project that reads named directories is not a tree. It may be a monorepo cut
into slices, or a thematic container collecting whole projects so one search
reaches all of them - `organization` is the type for that, and it is a label
for the reader rather than a rule the indexer enforces.

There are two shapes of it, and they are not the same thing. A monorepo cut
into slices reads directories: `project_sources.root_path` is unique, so a
directory belongs to exactly one project, and moving it there takes it away
from wherever it was.

An `organization` holds whole projects instead, by reference. Add members on
its overview tab: each one keeps its name, its `/mcp/<name>` address and its
own graph, is indexed once however many organizations list it, and belongs to
as many of them as it is relevant to. Searching the organization through
`search_code_nodes` searches every member, which is what it is for.

Because membership is a reference and not ownership, nothing follows it
quietly: a project that an organization lists refuses to be dropped, absorbed,
or moved into another project until it is taken out. Its page names the
organizations holding it, and taking it out is one button on either side.

A project of named directories has no root path of its own. `projects.root_path` names a tree
only when the project is one - a single unnamed directory, mounted whole - and
a container keeps the synthetic `registered://<name>` it was registered with,
so no slice stands in for the whole. The dashboard lists the directories
instead, and the worker API finds a project from a host path through its
sources rather than through that column.

## Indexing a project that reads nothing

An index run refuses a project with no directory rather than adopting the path
it was onboarded from:

```text
project 'mono' reads no directories yet; add one with
`make source-add PROJECT=<host path> PROJECT_NAME=mono ALIAS=<alias>`
```

A run also refuses when one registered directory is missing from the mount.
Indexing the rest would walk none of its files and prune every node it had, so
the fix is `make mounts` on the host and a restart of the API.
