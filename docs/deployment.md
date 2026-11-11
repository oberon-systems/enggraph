---
layout: default
title: Deployment
nav_order: 4
---

## Deployment

All services are defined in `docker-compose.yaml`, run as a **singleton
stack**: one database container serves every indexed codebase. The compose
project name is pinned (`name: enggraph`) and the data directory
is fixed at `~/.local/share/enggraph/db` - neither is configurable, on
purpose. Running a second stack from this compose file corrupts the data
directory; there is no per-stack path to keep two apart.

## Configuration

Copy `.env.example` to `.env`. `make up` refuses to run without it.

| Variable                 | Default    | Purpose                                                                                         |
| ------------------------ | ---------- | ----------------------------------------------------------------------------------------------- |
| `PROJECT_PATH`           | empty      | Host path a one-shot `indexer` run records; nothing else reads it                               |
| `PROJECT_NAME`           | derived    | Name the codebase is stored and addressed under; defaults to the last segment of `PROJECT_PATH` |
| `POSTGRES_PASSWORD`      | required   | Password of the cluster's superuser; compose fails fast when unset                              |
| `POSTGRES_USER`          | `user`     | The superuser, which owns the schema; only the one-shot jobs connect as it                      |
| `POSTGRES_APP_PASSWORD`  | generated  | Password of the role the services connect as; `make` writes one when it is empty                |
| `POSTGRES_APP_USER`      | `enggraph` | That role: it reads and writes rows and holds no DDL                                            |
| `POSTGRES_DB`            | `context`  | Database name                                                                                   |
| `GATEWAY_PORT`           | `3000`     | The one published host port; nginx routes it to every service                                   |
| `GATEWAY_BIND`           | `0.0.0.0`  | Interface the entry point binds. `127.0.0.1` keeps the whole stack off the network              |
| `GATEWAY_HOSTS`          | derived    | `Host` header values the gateway and the services behind it accept, comma separated             |
| `TAG`                    | `latest`   | Tag applied to images built by `make build`; compose only ever runs `:latest`                   |
| `INDEX_SCHEDULER`        | on         | Whether the worker API indexes on a schedule at all. `0` leaves every project to the button     |
| `SCHEDULER_TICK_SECONDS` | `30`       | How often it looks for a project that is due                                                    |
| `SUMMARY_WORDS_BATCH`    | `50000`    | Summaries turned into search words per tick while a project is first filled                     |
| `INDEX_MAX_RUNNING`      | `1`        | Index runs the worker API does at once; the rest queue in memory. They share one memory limit   |
| `AUTH_ENFORCE`           | `on`       | `off`, `warn` or `on`; see [Authentication](#authentication)                                    |
| `AUTH_OIDC_ISSUER`       | empty      | The OIDC issuer people log in through; empty is local mode                                      |
| `AUTH_OIDC_INTERNAL_URL` | empty      | Where the dashboard reaches the issuer when its public address does not resolve in a container  |
| `AUTH_OPERATORS`         | empty      | Subjects, or emails the issuer says are verified, whose OIDC login makes them an operator       |
| `MCP_QUERY_TIMEOUT_MS`   | `60000`    | The longest one statement of an MCP tool call may run before PostgreSQL ends it                 |
| `AUTH_PUBLIC_URL`        | derived    | The address browsers reach the gateway at; derived from `Host` and `X-Forwarded-Proto`          |

`PROJECT_PATH`/`PROJECT_NAME` are arguments to the indexing job only - they
name the tree one such run records, and never reach the compose project or the
other containers.

The dashboard's project list reads node, edge and file counts from a hash the
worker API keeps in Valkey. Every finished index run rewrites its project's
entry. A project missing from the hash, after a Valkey restart or an
eviction, is counted live once, and the worker API stores its counts again in
the background.

The schedule itself is per project and lives in the database, not here - see
[Indexing on a schedule](usage.md#indexing-on-a-schedule). One host limit
belongs on this page though: `auto` mode has the agent watch the tree on its
host, and the kernel caps how many directories may be watched at once.

```bash
sysctl fs.inotify.max_user_watches
```

The default is generous on most distributions and not on all of them. A watch
the kernel refuses is logged by the worker API, and the projects it covered
fall back to their interval - so `auto` degrades to `periodic` rather than to
nothing. Raise it on the host if you would rather have the watch:

```bash
sudo sysctl -w fs.inotify.max_user_watches=524288
```

## Resource limits

The whole stack shares one CPU and memory budget: `STACK_CPUS` (default `4`)
and `STACK_MEM` (default `6g`). `make` splits it into a share per service and
passes every share to compose, so no container can take the whole host. Memory
is rounded down to a power of two per service.

| Service      | CPU share | Memory share | At 4 CPU / 6g   |
| ------------ | --------- | ------------ | --------------- |
| `postgres`   | 30%       | 34%          | 1.2 CPU / 2048m |
| `worker-api` | 30%       | 34%          | 1.2 CPU / 2048m |
| `embedder`   | 15%       | 10%          | 0.6 CPU / 512m  |
| `valkey`     | 5%        | 10%          | 0.2 CPU / 512m  |
| `mcp-server` | 8%        | 4%           | 0.32 CPU / 128m |
| `viewer`     | 5%        | 3%           | 0.2 CPU / 128m  |
| `web`        | 4%        | 3%           | 0.16 CPU / 128m |
| `nginx`      | 3%        | 2%           | 0.12 CPU / 64m  |

Postgres gets a quarter of its memory as `shared_buffers`, and Valkey keeps
three quarters of its own as `maxmemory`. To see the split for the current
settings:

```bash
make limits
```

A per-service value in `.env` wins over its share: `PG_`, `API_`, `EMBEDQ_`, `SUMMARIZE_`, `EMBED_`,
`VALKEY_`, `MCP_`, `VIEWER_`, `WEB_` or `NGINX_` followed by `CPUS` or `MEM`,
plus `PG_SHARED_BUFFERS` and `VALKEY_MAXMEMORY`. `EMBEDQ_` and `SUMMARIZE_`
are the two queue services, `EMBED_` the embedding model. The one-shot
`indexer` container sits outside the budget and keeps `INDEXER_CPUS` and
`INDEXER_MEM`.

Check the limits after `make restart`:

```bash
docker stats --no-stream
```

The `MEM USAGE / LIMIT` column shows the share of each service, not the host
total.

## The entry point

nginx is the only service that publishes a host port. It picks the backend
from the request path:

| Path                                  | Served by  | What it is                                      |
| ------------------------------------- | ---------- | ----------------------------------------------- |
| `/mcp`, `/mcp/<project>`              | mcp-server | Streamable HTTP, the transport to use           |
| `/sse`, `/sse/<project>`, `/message`  | mcp-server | the older SSE pair                              |
| `/health`                             | mcp-server | what `make status` probes                       |
| `/worker/...`                         | worker-api | the remote summarization queue, prefix stripped |
| `/auth/login`, `/callback`, `/logout` | web        | logging in and out of the dashboard             |
| `/auth/v1/...`                        | rauthy     | the identity provider, under the `auth` profile |
| `/agent/agents/...`                   | worker-api | host agents, which sign every request           |
| everything else                       | web        | the dashboard, its API and the graph page       |

The config is `nginx/default.conf`, bind-mounted read-only. Two details in it
are load-bearing rather than stylistic. Every `proxy_pass` goes through a
variable so that names resolve per request: `worker-api` sits behind the
`remote` profile, and a static upstream name that does not resolve makes nginx
refuse to start instead of answering 502 for that one path. Response buffering
is off on the MCP routes, because both transports hold a response open and a
buffered stream reaches the client only when the session ends.

The viewer is not routed directly. The dashboard already proxies `/graph` and
`/vis-network.min.js` same-origin, and the viewer emits absolute root paths, so
it cannot be mounted under a prefix.

`GATEWAY_BIND` defaults to every interface, which is what a summarization
worker or an agent on another machine needs. Until `AUTH_ENFORCE=on` it also
puts the dashboard and the MCP server within reach of the local network
unauthenticated, and `make up` warns about it. Set `GATEWAY_BIND=127.0.0.1` if
you run no remote worker.

## Authentication

Every request through the gateway is asked about first. nginx sends an
`auth_request` to the dashboard's `/auth/check`, which names the principal - a
person by a session cookie, an agent or a script by an enggraph token - and
answers `204`, `401` for nobody or `403` for a principal that may not do this.
The check lives in enggraph rather than in the identity provider, so any OIDC
issuer will do.

| `AUTH_ENFORCE` | Anonymous request                   | Use it                                    |
| -------------- | ----------------------------------- | ----------------------------------------- |
| `off`          | passed; nothing is asked            | to switch the check off entirely          |
| `warn`         | passed, logged by method and path   | first, to see what still runs anonymously |
| `on`           | refused: pages go to the login form | the default                               |

With `off` nginx answers the check itself, so the gateway does not depend on
the dashboard; `warn` and `on` ask it on every request. Read what `warn`
logged with `make web-logs`. Exempt from the check are
`/health`, `/nginx/health`, the login and callback addresses, the static
assets of the login page, Rauthy's `/auth/v1/` and `/agent/agents/`, where an
agent's signature does the job.

### Tokens

A token reads `egt_<id>_<secret>` and is sent as
`Authorization: Bearer egt_...`. Only the sha256 of the secret is stored, so
the text is shown once, when it is issued. A token carries a kind (`operator`,
`mcp`, `api`), a role (`operator` or `reader`), scopes (`read`,
`write-records`, `admin`) and the projects it may read, or `*`.

- An MCP call naming a project outside the token's list is refused, and so is
  a call that reads across every project. A write tool needs the `operator`
  role or the `write-records` scope.
- The dashboard needs a token for every project. A reader may read it; a
  write, the Ask page and the Tokens page are for operators.
- On `/worker/` the gateway replaces `Authorization` with the internal
  `WORKER_API_TOKEN` once the check passed, so that token never leaves the
  stack. A remote summarization worker carries an `api` token with the
  `write-records` scope instead. The worker API weighs no project, so a
  token held to some projects is refused there, whatever it asks for. Past
  the job routes (`/worker/jobs`, `/worker/tasks/` and the project list) the
  worker API is an operator's: registering, renaming and dropping projects,
  file text and the server probes.
- A worker that reaches the worker API directly, with enforcement off, is
  given `WORKER_QUEUE_TOKEN`, which opens those job routes alone, and never
  `WORKER_API_TOKEN`, which opens all of it.

Operators issue and revoke tokens on the dashboard's Tokens page, or inside
the stack:

```bash
make token WHO=alpha-laptop
make token WHO=summarizer KIND=api SCOPES=read,write-records
make token WHO=admin KIND=operator
```

The token is printed once. `docker compose exec web python -m
enggraph.core.authn list` lists the tokens and `revoke <id>` ends one, with the
sessions it opened.

### Local mode

With `AUTH_OIDC_ISSUER` empty, the login form takes an operator token and
opens a session for it: 12 hours sliding, 7 days at most, in an HttpOnly
`eg_session` cookie. Issue the first one with `make token WHO=<name>
KIND=operator`.

### Upgrading an existing install

`on` is the default, so after the upgrade everything that reached the stack
without a token is refused: agents, scripts, a remote summarization worker,
a browser that has not logged in. To keep the old behaviour, put
`AUTH_ENFORCE=off` into `.env`. To move over, start in `warn`:

```bash
echo AUTH_ENFORCE=warn >> .env
make up
make token WHO=admin KIND=operator
```

1. Log in at `/auth/login` with that operator token.
2. On every machine an agent runs on, issue a token, put it into the `env`
   block of Claude's `settings.json` as `ENGGRAPH_TOKEN` and run
   `make install` again in each codebase; see
   [Onboarding](onboarding.md#the-agent-token).
3. Give a remote summarization worker an `api` token with `write-records` as
   its `--token`; see [Summarization](summarization.md).
4. Watch `make web-logs`. Once it names no anonymous request that should not
   be there, drop the `AUTH_ENFORCE` line from `.env` and run `make up`.

### Rauthy

[Rauthy](https://github.com/sebadob/rauthy) ships in compose under the `auth`
profile, served at `<gateway>/auth/v1`. Its data lives in a database `rauthy`
on the same PostgreSQL server, created on its first start, and
`make backup` takes it along. Set these in `.env`, then start it:

```text
COMPOSE_PROFILES=auth
AUTH_OIDC_ISSUER=http://localhost:3000/auth/v1
AUTH_OIDC_INTERNAL_URL=http://rauthy:8080/auth/v1
AUTH_OPERATORS=admin@localhost
RAUTHY_SECRET_RAFT=<at least 16 random characters>
RAUTHY_SECRET_API=<at least 16 random characters>
RAUTHY_ENC_KEYS=<id>/<base64 of 32 random bytes>
RAUTHY_ENC_KEY_ACTIVE=<id>
```

```bash
make up
make auth-secret
make restart
```

On its first start Rauthy creates the `enggraph` client from
`rauthy/bootstrap/clients.json`, with the redirect URI
`http://localhost:3000/auth/callback`; another gateway address means editing
that file before the first start, or the client in Rauthy's admin UI after
it. `make auth-secret` writes the client's id and secret under `secrets/`,
owned by the dashboard's user (uid 10001) and readable by nobody else. The admin's password is in `make logs` on the
first start. Any other issuer - Keycloak, Authentik, Zitadel - replaces
Rauthy through `AUTH_OIDC_ISSUER` and the two files under `secrets/`.

An entry of `AUTH_OPERATORS` is a subject or an email. An email counts only
when the issuer sends `email_verified: true` with it, so prefer the subject
where the issuer lets accounts carry an address nobody checked. The role is
settled at every login: someone taken off the list is a reader from the next
one, and the sessions opened as an operator end then.

### Host agents

A host agent serves its directories to the stack, read-only, over a socket it
opens itself. [Host agent](agent.md) covers installing it. It registers and gets a one-time claim code, an operator approves
it on the dashboard's Agents page, and the claim hands over a credential. Every
later request is signed with a key derived from that credential and a master
key, so the stack stores no agent key.

The stack makes the master key itself the first time an agent needs it and keeps
it in the database, in `agent_master_keys`. There is nothing to write or copy, and
a whole-database backup carries it. Whoever holds that backup can forge agent
credentials, so guard it as you guard your tokens.

To rotate the key, name a new one in `.env` and apply that. The stack makes it,
and older keys keep verifying the credentials issued under them, which last an
hour.

```text
AGENT_CURRENT_KID=k2
```

```bash
make up
```

Approve an agent on the Agents page:

1. Compare the fingerprint shown there with the one the agent logged: the first
   8 hex digits of the sha256 of its claim code.
2. Approve the agent. A registration nobody decides lapses after 24 hours.
3. Approve each project it offers, separately. A project added later, or one
   whose path changed, waits for approval again.
4. Bind an approved project. It joins an existing project only when its path
   equals that project's `root_path`; otherwise it becomes a new project of its
   name. A path another project already reads gets the root
   `agent:<agent-name>:<host-path>`.

**Approve the import** beside an agent does steps 3 and 4 at once, for every
project it offers on the path of an unbound project.

A bound project is read through its agent at once. **Unbind** on the same page
leaves it with nothing to read it: it keeps its graph and gets no run. A removed, moved or
refused project loses its binding. A token bound to an agent on the Tokens page
makes `describe_project(path)` look among that host's bound projects first.

An agent that goes offline stops the project's runs and removes nothing from its
graph. Text search and file text answer nothing until the agent returns. A
reconnecting agent owes the project a full pass, which reads only what changed.

Files in the denied list are never read through an agent. A selected one such
as `.env.example` has no node in a project first indexed through an agent. A
project indexed before it was bound keeps the nodes it has.

| Control | What it does                                                                                |
| ------- | ------------------------------------------------------------------------------------------- |
| Disable | Refuses the agent's requests and closes its socket at the next ping                         |
| Enable  | The only way back for a disabled agent, including one a new claim was approved for          |
| Revoke  | Bumps the generation; the agent's next signed request disables it, and it must enroll again |
| Delete  | Forgets the agent and its bindings, not its projects; a running agent registers again       |

An agent still signing with a generation it was rotated past is disabled
automatically, because two parties hold that credential. Registering and
claiming are limited to 60 requests a minute per address, which answers `429`
past a burst of 20.

## Host header allowlists

The gateway answers a request only when its `Host` header is listed in
`GATEWAY_HOSTS`, or names the machine itself (`localhost`, `127.0.0.1`,
`[::1]`); any other name gets 421 from every route, `/health` and the worker
API included. That is what stops a page from rebinding its own name to the
gateway. Change `GATEWAY_PORT`, or reach the stack under any other name -
from a browser, an MCP client or a host agent - and `GATEWAY_HOSTS` has to
say so.

Behind the entry point the `Host` header carries the gateway's port, not the
port the service itself listens on, so the MCP server and the dashboard API,
which check it again, are given their allowlist by compose from the same
variable.

nginx passes `Host` through unchanged on purpose. The dashboard derives the
`Origin` it expects for a write from the `Host` it sees, so rewriting the
header would make every cross-site request look same-origin, and that check is
what guards an unauthenticated dashboard.

Two optional variables, read by the MCP server, implement DNS rebinding
protection (GHSA-w48q-cv73-mx4w). Compose
sets `ALLOWED_HOSTS` from `GATEWAY_HOSTS`; these are the raw variables behind
it:

| Variable          | Default                  | Purpose                                                              |
| ----------------- | ------------------------ | -------------------------------------------------------------------- |
| `ALLOWED_HOSTS`   | loopback names on `PORT` | Comma-separated `Host` header allowlist, or `*` to disable the check |
| `ALLOWED_ORIGINS` | empty                    | Comma-separated `Origin` header allowlist                            |

## Backup and restore

```bash
make backup                                  # the whole database
make backup PROJECT_NAME=api                 # one codebase
make backup PROJECT=/home/you/work/api       # the same, resolved by root path
make backup KEEP=20                          # keep more than the seven default
make backup KEEP=                            # keep everything, prune nothing
```

Files land in `~/.local/share/enggraph/backups`. A whole-database backup
is a `pg_dump` custom archive (`context-<timestamp>.dump`); a single-project
backup is a plain-SQL replay of that project's rows in foreign-key order
(`<name>-<timestamp>.sql.gz`), since `pg_dump` can't select by row. Neither
file gets its final name until it's been read back, so an interrupted dump
is never mistaken for a real backup.

Backups rotate independently per kind, keeping the newest `KEEP` (7 by
default); `FILE=<path>` writes elsewhere and is never pruned. With the `auth`
profile a whole-database backup also writes Rauthy's database beside it as
`rauthy-<timestamp>.dump`, which `make restore FILE=` puts back into
`rauthy`; stop the `rauthy` service first.

```bash
make restore FILE=context-20260819-115420.dump   # replaces the whole database
make restore FILE=api-20260819-115137.sql.gz     # replaces that one project
make restore                                     # lists what's in the backup dir
```

Both directions print what's about to be replaced and ask first
(`FORCE=1` skips the prompt), and both run inside a single transaction so a
failed restore can't leave a half-restored database.

Worth doing before dropping a project and before `make clean`: nodes, edges,
hashes and embeddings come back with one index run, but plans, manually
written summaries and everything in `_memory` don't come back from anywhere
else.

## Upgrading

Take a whole-database backup before every upgrade, without exception. A new
version can carry schema migrations, and `make up` applies them before any
service starts. A migration is not reversible: the only way back is the
backup taken before it.

An install on 0.x does not upgrade to 1.0.0 in one step: it goes through
0.24.0, as [BREAKING CHANGES](breaking-changes.html#upgrading-from-0x-to-100)
describes.

```bash
make backup
docker compose stop worker-api mcp-server web viewer
git pull
make build
make up
```

- `make backup` with no `PROJECT` writes a whole-database `.dump`. Check that
  the `wrote ...` line names that file and a plausible size before going on.
- The services are stopped so that no open connection holds a table a
  migration replaces.
- `make up` runs the `migrate` service first; the other services start only
  once it succeeds.
- A migration that copies a large table needs free space for the copy on the
  database volume until it commits.

Check the result:

```bash
make status
docker compose logs --tail 5 migrate
```

`make status` shows the schema revision the database is at, and the
`migrate` log names every revision it applied: `Running upgrade  -> 0001`.
A log with no such line means nothing was pending.

When a migration fails, its transaction rolls back: the database stays at the
version it had, and the services stay down. Send the `migrate` log with the
report. To go back instead, check out the previous release, `make build`, and
`make restore FILE=<the .dump taken above>`.

## Database roles

The cluster holds two roles, and the services use one of them.

- **`POSTGRES_USER`** created the cluster, so it is a superuser and owns the
  schema. Only the one-shot jobs connect as it: `migrate`, which applies the
  revisions, and `rauthy-db`. `make backup`, `make restore` and `make psql`
  use it through the socket inside the `postgres` container.
- **`POSTGRES_APP_USER`** is what every service and Rauthy connect as.
  `migrate` creates it, sets its password from `POSTGRES_APP_PASSWORD` at
  every run and grants it the rows of every table: `SELECT`, `INSERT`,
  `UPDATE` and `DELETE`, with no DDL. It is no superuser, so it reads no file
  of the server and starts no program there.

Rauthy migrates its own database, so `rauthy-db` hands the `rauthy` database
and its tables over to that role. A restored `rauthy-*.dump` belongs to the
superuser again until the next `make up`, which runs `rauthy-db`.

To change the password, change `POSTGRES_APP_PASSWORD` in `.env` and run
`make up`.

## Pinned artifacts

Nothing the stack runs or installs is taken by a name that can move.

- **Third-party images** are named by tag and digest in `docker-compose.yaml`
  and in every `Dockerfile`. Moving one is a change to that line.
- **The stack's own images** are `:latest` in the repository's compose file,
  which is the one a checkout builds for. A release writes
  `docker-compose.release.yaml`, where each is named by version and digest; it
  is attached to the release and kept with the workflow run. Start a deployed
  stack from that file.
- **Python packages** come from hashed requirement files, installed with
  `--require-hashes`: `packages/<image>/requirements.txt` for an image, the
  build backend included, and the three `requirements-*.txt` at the root for
  the virtualenv. They are written by `make lock` (it needs
  [uv](https://docs.astral.sh/uv/)) from the pins in each `pyproject.toml` and
  in `requirements.toml`. After changing a pin, run `make lock`; the test
  suite fails while a pin and its file disagree.

## GitHub Pages Setup

This documentation is published via GitHub Pages.

- **Enable Pages:** repository **Settings** -> **Pages** -> set
  **Build and deployment** -> **Source** to "GitHub Actions".
- **Automatic build:** `.github/workflows/pages.yml` builds and deploys
  `/docs` on every push to `main`.
- After the first push, the site is live at
  `https://<owner>.github.io/<repo>/` within a few minutes.
