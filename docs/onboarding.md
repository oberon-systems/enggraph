---
layout: default
title: Onboarding
nav_order: 2
---

## Prerequisites

- Docker with the Compose plugin.
- Python 3.11+ (for internal tools).

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

   This installs the [host agent](agent.md) when it is missing and offers the
   tree. Approve the agent and the project on the dashboard's Agents page, then
   bind the project, and the run finishes by itself with compose untouched. It
   does not build the graph. The dashboard lists the project as `never indexed`
   with an Index button beside it, and that button is what indexes it.
   `AGENT_ROOT` defaults to the directory `make` is called from, so
   `make -C /path/to/enggraph install` run inside the codebase needs no
   `AGENT_ROOT` at all.

   `INSTALL_MODE=mounts` onboards through a read-only mount in the compose
   override instead, until that override is removed.

## The agent token

The gateway refuses an agent without a token (`AUTH_ENFORCE=on`, the
default). An operator issues one on the dashboard's Tokens page, or inside the
stack:

```bash
make token WHO=alpha-laptop SCOPES=read,write-records
```

`make install` writes the `enggraph` entry with the header
`Authorization: Bearer ${ENGGRAPH_TOKEN}`, so no file in the codebase holds
the token. Put the token into the `env` block of Claude's `settings.json`
(`~/.claude/settings.json`, or the one under `CLAUDE_CONFIG_DIR`), the way
any other MCP token is kept there:

```json
{
  "env": {
    "ENGGRAPH_TOKEN": "egt_..."
  }
}
```

Claude Code refuses the whole `.mcp.json` while the variable is unset, so set
the token before running `make install`. An existing entry pointing at this
stack gains the header only when it has no `Authorization` header of its own.
Gemini takes the same variable from `~/.gemini/.env`. `write-records` lets
the agent save memories, plans and issues; leave it out for a token that only
reads.

## Adding new codebases

You can index multiple codebases into the same database stack:

```bash
make install AGENT_ROOT=/path/to/another/project
make install AGENT_ROOT=/path/to/a/handbook TYPE=docs
```

Each one waits for a decision on the Agents page. The stack manages them by
path, and you can access them by name via MCP.
`TYPE=` categorises a project - `codebase` (the default), `docs` or `config` -
which is what `search_code_nodes` narrows on when it searches every project at
once. It is stored with the row, so a later run without `TYPE=` keeps it.
Names beginning with `_` are refused: they belong to the built-in projects,
`_memory` today.

Onboarding a tree twice is safe. No file that exists is replaced, the agent
offers the directory once, and the project row keeps the type and the index date
it already had.

To move projects that already read through a mount, see
[Move the existing projects](agent.md#move-the-existing-projects).

## Keeping skills current

Nothing installs the skills into a codebase: the MCP server hands them out.
`skills/` is baked into the api image and `worker-api` imports it into
the database at start, so a rebuilt stack carries the new skills. Each session
is told the skills it should have and their versions, the sha256 of each text,
in the server instructions and again with the answer to its first tool call.
An agent whose copy under `.claude/skills/<name>/` is missing or carries
another `version:` fetches it with `get_skill` and writes it there.

The _Skills_ page lists every skill and imports new ones - a SKILL.md for
everyone, one organization or one project. Built-in skills are read-only
there. A project's or an organization's _skills_ tab switches which ones it
gets; `enggraph` is always on. A switch left unset follows an organization
holding the project, and otherwise built-in skills and the project's own are
on.

## Several projects, one organization

A project is one tree, and grouping several of them is what an `organization`
is for. It reads no tree of its own and holds other projects by reference, so
one search reaches every member while each keeps its name, its
`/mcp/<name>` address and its own graph.

Register the organization first, then the projects it will hold:

```bash
make install AGENT_ROOT=/home/you/work/mono SOURCE=none TYPE=organization
make install AGENT_ROOT=/home/you/work/mono/deploy/configs
make install AGENT_ROOT=/home/you/work/mono/tools/agents
```

`SOURCE=none` writes the agent files and the project row without
a tree behind it. The two runs after it are ordinary projects: each is a tree
of its own, mounted at `/code/<project>` and indexed on its own.

## Which organization holds a project

Membership is settled in the dashboard rather than on the command line,
because it is a decision about two projects rather than a setting on one.

On the organization's overview tab, under _Members_:

- _Add member_ lists a project there while it stays a project of its own,
  beside the others.

On a project's own page, under the organizations it belongs to:

- _Add to it_ is the same thing from the other side: one more organization,
  and the project stays in the projects list.
- _Move into it_ makes that organization where the project is listed. It
  leaves the projects list, which is what `org_members.owned` records,
  and taking it out puts it back with everything it has.

Neither moves a file. The tree, the mount, the node ids and the graph of a
member are not what its membership is about, and nothing is indexed again for
having joined.

## Organizations

An `organization` is a project that holds other projects rather than files.
`projects.root_path` names a tree only when the project is one, so an
organization carries the synthetic `registered://<name>` it was registered
with and is never indexed itself.

Membership is a row and nothing else: a member keeps its name, its
tree, its mount, its node ids, its `/mcp/<name>` address and its graph exactly
as they were, is not indexed again for having joined, and belongs to as many
organizations as it is relevant to. Its own page adds it to an organization or
moves it into one, and the difference is where the project is listed. Added,
it stays a project of its own beside the others and belongs to as many
organizations as are relevant to it. Moved in, that organization is where it
lives: it leaves the projects list and is listed there instead, which is what
`org_members.owned` records. A project some organization already holds is
not moved by either - leaving one is taken by taking it out, on either page,
and a project moved in goes back to the list with everything it has.

Naming the organization is what reads it. Every read does: `search_code_nodes`,
the graph reads, the plans, the memories and the suggestions all cover every
member at once and say which one answered, and `describe_project` lists the
members with the sentence written about each - which is how a session opened on
`/mcp/<organization>` learns what it can reach rather than reading an empty
graph and concluding the tree was never indexed. Writing is not: a summary and
a file hash belong to a graph, and the call names the member instead.

A project is renamed from its own page, and the name is the only thing that
changes. No table holds a key, so the rename itself re-keys every table
naming the project where it stands: the graph, the chunks, the settings, the
memberships, the links, the index runs and the records written about the old
name. No tree is read again and no node id changes - a node id is
relative to the tree, not to the project. What the database cannot carry is
outside it: run `make mounts` and restart the services, because the mount is a
file on the host, and change the `.mcp.json` of any codebase onboarded
against
`/mcp/<the old name>`. The button asks for the current name before it does any
of it.

Each project is described by a sentence of its own, written on its page in the
dashboard and stored in `projects.description`. It is what an organization
lists beside every member it holds, so an agent choosing between them has
something to choose by; nothing derives it from the tree, and no index run
touches it.

Pressing _Index_ on an organization starts one run per member that is not
turned off, and the reply folds them into one answer: running while any of
them is, failed if any of them failed, and the counts summed. A member already
indexing is skipped with its reason rather than refusing the whole fan-out.

An organization is also a settings level. Its members inherit the indexing
schedule it sets and override it with rows of their own: the project decides
for itself first, then the organizations it belongs to in the order it joined
them, then the global default. Its ignore lines are not overridden but added
to: a member prunes what the global default, each of its organizations and
the member itself name. Its members table lists each one with a way into its own settings,
because a member is a project in its own right and that is where its rows are
written.

Because membership is a reference and not ownership, nothing follows it
quietly: a project that an organization lists refuses to be dropped or moved
into another organization until it is taken out. Its page names the
organizations holding it, and taking it out is one button on either side.

The members table says how each one is kept: which level its indexing settings
were resolved at, whether it is swept on a timer or watched, and how long ago
it last ran - green under half an hour, amber up to an hour, red past it. A
member that indexes only by hand reads `off` instead, because none of those
answers means anything for it.

## Indexing a project that reads nothing

An index run refuses a project with no tree rather than adopting the path it
was registered from:

```text
project 'mono' reads no directory yet; give it one on its own page in the
dashboard before indexing it
```

A run also refuses when the tree is missing from the mount. Indexing anyway
would walk none of its files and prune every node it had, so the fix is `make
mounts` on the host and a restart of the API.
