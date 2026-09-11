---
layout: default
title: Embedding
nav_order: 6
---

## What the vector half adds

`search_code_nodes` matches identifiers. It answers "where is `AuthService`"
perfectly and "where is the retry logic" not at all, because nothing in the
graph is named `retry logic`.

`search_code` answers the second kind of question. It runs two searches and
combines them:

| Half     | Reads                                       | Finds               |
| -------- | ------------------------------------------- | ------------------- |
| lexical  | node names, node ids, summaries, chunk text | the words you typed |
| semantic | the chunk vectors, by cosine distance       | what you meant      |

The two are ranked separately and fused by rank, so a file both halves found
outranks one only the vector half did. Each row names the file and the line
range to read.

The lexical half needs nothing but the graph. The semantic half needs the
files embedded, and that is what the rest of this page is about. Until they
are, `search_code` answers with the lexical half alone and says so in the
reply rather than pretending to be complete.

## Turning it on

Three steps, in any order. The migration that creates the tables is part of
`make db migrate` and is assumed applied.

```bash
make llm-model-install MODEL=nomic-embed  # once: 274 MB of weights
make up EMBED=1                           # start the embedder container
```

Then open the dashboard, go to Settings, and turn **Embedding** on. Each
feature is one row - the switch, the server it dials, and one button:

```text
Settings -> Embedding

[ (o__) on ]  [ http://embedder:8080 ]  [ Test ]
```

The button reads **Test** while an address has been typed that nobody has
dialled yet, and **Save** once it has answered. That order is the point: an
address stored without answering is found out by a queue going quiet an hour
later. On a stack with no GPU, type `http://embedder:8080`: the queue only
ever works against a named server, never against the container by default.

Watch it work in the worker API log:

```bash
make api-logs
```

Expect `Queued N file(s) of <project> for embedding` within a tick of the
switch, and the counters on the project's settings tab to start moving.

## The switches

Two of them at the global level, because one field cannot hold two questions:

- **enabled / disabled** - whether embedding may run at all. Disabled is the
  kill switch: no project is asked and no project's own setting is consulted,
  which is what makes it safe to shut the model down without visiting each
  project first.
- **status: on / off** - what a project that has said nothing about itself
  does. It is a default, and a project may state the opposite.

A project's own tab has the second one only. Left alone it reads
`inherited from global: on` with a dashed track - the position resolved from
above, which is not the same as this level having chosen it. Touching it
stores a choice; the **Inherit** button beside the row clears the whole row.

Every other field inherits the same way, one field at a time. An empty field
shows in grey what it inherits and from where, for example
`inherited from global: http://192.0.2.50:8085`, and the line under the row
names the URL in force. Emptying a field and saving clears that field alone.

```text
disabled                  -> off everywhere, nothing else is read
enabled + status: on      -> on for every project that says nothing
enabled + status: off     -> off for those, and any project may say otherwise
```

That last line is the case worth remembering: to embed one project and no
others, leave embedding enabled, set the global status to off, and turn it on
from that project's own settings tab.

The switches live in `project_settings.settings` beside the indexing
schedule, at the same three levels: the project, the organizations holding it,
then the global default. The enabled/disabled half is the `allowed` field and
is read from the global level alone - a project cannot allow itself something
the operator switched off.

Turning the status off stops the queue within a tick. The vectors already
written stay, and `search_code` goes on using them.

## Where the model runs

The model is a server speaking the OpenAI embeddings route, which is what
`llama-server` implements.

There is one primary: the URL stored in the settings, or `EMBED_SERVER_URL`
when no level stores one. Who may use which server:

| Caller                  | Primary | `EMBED_LOCAL_URL` (the `embedder` container) |
| ----------------------- | ------- | -------------------------------------------- |
| the queue, `make embed` | yes     | never                                        |
| a `search_code` query   | yes     | only when the primary cannot be connected to |

The queue never falls back. A queue drained on a CPU because the GPU was away
is how work quietly moves onto a CPU while somebody watches an idle GPU. With
the primary down the queue waits: files go back with their attempt returned,
`failed` does not grow, and the queue resumes by itself once it answers.

A query must answer now, so it falls back - but only on a connection that was
never made. A primary that is connected and slow is not a reason to ask
another model; the query answers with its lexical half instead. The local
server must run the same model, and it never receives the primary's token.

The local server is optional, and a stack without it is normal. An address a
query could not connect to is skipped for `EMBED_PROBE_SECONDS`, so with the
primary down and no local server a search does not wait on either: it answers
with its lexical half at once.

On a machine with a GPU, `worker\start-llama-embeddings.bat` starts the
server with the flags this route needs. All three matter:

| Flag                 | Without it                                          |
| -------------------- | --------------------------------------------------- |
| `--embeddings`       | 501, "does not support embeddings"                  |
| `--pooling mean`     | 400, "Pooling type 'none' is not OAI compatible"    |
| `--ubatch-size 8192` | 500, "input is too large to process" on long chunks |

A chat server cannot serve this route whatever the flags are: its vectors are
a different width, and this column stores 768. That is a second process, on a
second port, beside the one summarizing uses.

**Test** dials the address and reports the model and the dimensions it
answered with. Summarizing has a button of its own beside its own field: a
server that completes a sentence and a server that returns a vector are two
different things, and dialling the wrong route would prove nothing.

A server published on a LAN is started with `--api-key`, and the token goes in
the field beside the URL. It is write-only: it is stored, it is sent as
`Authorization: Bearer`, and it never travels back to a browser - the
dashboard has no authentication of its own, so a secret rendered into the page
would be readable by anyone who can open it. What the page shows instead is
`token set, renew by <date>`, and `token expired, please renew` in red once
the token is 30 days old. Expired means due for rotation and nothing else: the
key goes on being sent, because a queue that stopped itself on a date would be
exactly the silent failure these switches exist to prevent. To replace a
token, type the new one over the empty field and save; a wrong one is reported
by Test as `refused the token (401)`, which is a different problem from an
address that did not answer.

A GPU is optional. The `embedder` container runs llama.cpp on CPU; to have it
embed the queue as well as answer queries, store `http://embedder:8080` as the
URL.

## Timeouts

Every request to a model server has four timeouts, fixed in
`graphify/src/enggraph/config.py` rather than in `.env`:

| Request              | Connect | Write | Read  | Total |
| -------------------- | ------- | ----- | ----- | ----- |
| a queue batch        | 1 s     | 5 s   | 120 s | 180 s |
| a search query       | 1 s     | 2 s   | 3 s   | 4 s   |
| a summary            | 1 s     | 5 s   | 240 s | 300 s |
| Test, and the probes | 1 s     | 5 s   | 10 s  | 15 s  |

The one-second connect is what makes a dead server cheap: it is found out at
once, left alone for `EMBED_PROBE_SECONDS`, and never mistaken for a server
busy with the work. A queue batch that runs out of read time is halved and
tried again. The query total stays under the 5 s the MCP server waits.

## What it costs without a GPU

Two very different workloads sit behind one model.

A **search query** is one forward pass over about ten tokens: roughly 50 to
100 ms on two CPU threads. This happens on every `search_code`, and CPU is
comfortable with it.

The **first pass over a tree** is the expensive one. A chunk is about 1400
characters, near 390 tokens, and a forward pass over it is around 100 GFLOP
against the 40 to 60 GFLOP/s two CPU threads deliver - so budget on the order
of a second per chunk. For this repository that is 186 files and 1298 chunks,
so 20 to 40 minutes. These are estimates from the parameter and token counts
rather than measurements; the real figure is visible in the log once the
weights are installed.

The queue is throttled below that anyway: `EMBED_TASKS_PER_TICK` files every
`EMBED_TICK_SECONDS`, four per twenty seconds by default, which is twelve
files a minute and is meant to stay out of the way rather than to finish
quickly.

Levers, when it is too slow:

- `EMBED_THREADS` - the default is 2, and this scales close to linearly.
- `EMBED_TASKS_PER_TICK` - how much of the machine the background queue takes.
- `make embed` - the same work in the foreground, without the per-tick budget,
  for filling a large tree while you watch.
- `EMBED_SERVER_URL` - a GPU machine, for the first pass only.

```bash
make embed PROJECT_NAME=alpha   # one project
make embed                      # every project that asked for it
make embed BG=1                 # detached, for a large tree
```

The cost is per file and paid once. Every chunk records the hash of the file
it was cut from, so a re-index queues only the files whose hash moved - after
the first pass that is a handful of files per commit.

## How the queue works

An index run that finishes enqueues the files whose vectors are not current.
A project switched on is enqueued within a tick, and a sweep every few minutes
catches anything else.

The loop is a thread of the worker API, because that is the one service
holding the read-only mounts and therefore the only one that can read the file
a chunk is cut from. Each tick it takes a few files under a lease, splits them
into overlapping windows, embeds them a batch at a time and replaces that
file's rows in one transaction.

Two failures are told apart deliberately. A file this stack cannot read is
recorded on the task and retried at most `EMBED_MAX_ATTEMPTS` times. **No
server answering is not a failure**: the file goes back to the queue with its
attempt returned, and the reason is logged once rather than once per file - so
an afternoon with the model switched off costs nothing and needs no cleanup.
A dead server stops only the projects pointed at it; the others go on draining.

A project the database refuses to enqueue is logged by name in the worker-api
log as `Could not queue <project> for embedding`, and the sweep moves on to the
next one. The index run that tried is closed either way, so a failed enqueue
never holds the project's next run back.

## Checking on it

The **Queues** page in the dashboard is the short answer: both queues side by
side, a percent each, and a row per project saying how much is summarised, how
much is embedded and how much each still owes. It refreshes itself every ten
seconds, which is faster than either queue ticks.

A project's settings tab reports the same for that project alone. Over HTTP,
for every project at once:

```bash
curl -s http://127.0.0.1:3000/api/embeddings | python3 -m json.tool
curl -s http://127.0.0.1:3000/api/summaries | python3 -m json.tool
```

Each row also carries which level decided the switch and which addresses would
be tried, which is usually enough to explain a queue that is not moving.

A file is taken at most three times (its task's `attempts`). The third
failure sets the embed bit of the file's `skip` mask (`metadata.skip`, 1 for
summarize, 2 for embed) with the reason beside it, and the file is out of the
queue and out of the percent from then on. The Queues page shows the count in
red; it opens the project's **failures** tab, which lists the files and why.
The retry icon beside it clears the bit for that project, and the one in the
page header clears it for every project. A minified bundle is the usual case -
one line too long to embed - and excluding it from the selection is the
lasting fix.

The percent is green at 100, yellow while files are still queued, and red when
nothing is queued, it is short of 100 and files were given up on.

## Nuances

- **The model is part of the schema.** The vector column is declared at 768
  dimensions for `nomic-embed-text-v1.5`. Another model of another width needs
  a numbered migration, not a variable, and every existing row would have to be
  written again.
- **Quantization changes the vectors.** The catalogue ships f16 for this
  reason: the file is small either way at 137M parameters, and a quantized
  model would place its answers slightly differently from the rows already
  written by an unquantized one.
- **A row records its model.** Vectors written by another model are stale
  however fresh the file is, and are re-queued rather than searched.
- **Empty files get no vector at all.** A vector of a model's opinion of
  nothing would match every other nothing. They still count as embedded, so
  the percent reaches 100.
- **One very long line is one chunk.** A minified bundle is not split
  mid-token; it becomes a single oversized chunk, which is why generated files
  are better excluded by the selection than embedded.
- **Dropping or renaming a project takes its vectors with it**, through the
  same cascades the graph uses. Nothing needs cleaning by hand.

## Where the code is

| Piece                     | File                                  |
| ------------------------- | ------------------------------------- |
| the switch and its levels | `graphify/src/enggraph/features.py`   |
| chunking                  | `graphify/src/enggraph/chunks.py`     |
| the embedding client      | `graphify/src/enggraph/embedder.py`   |
| the queue                 | `graphify/src/enggraph/embedjobs.py`  |
| the background loop       | `graphify/src/enggraph/embedloop.py`  |
| the foreground pass       | `graphify/src/enggraph/embed.py`      |
| the tool                  | `mcp-server/src/index.ts`             |
| the tables                | `migrations/0020_embedding_queue.sql` |
