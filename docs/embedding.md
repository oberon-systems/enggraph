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
later. Leave the field empty to use the container, and the button is Save from
the start.

Watch it work in the worker API log:

```bash
make api-logs
```

Expect `Queued N file(s) of <project> for embedding` within a tick, and the
counters on the project's settings tab to start moving.

## The switches

Two of them at the global level, because one field cannot hold two questions:

- **enabled / disabled** - whether embedding may run at all. Disabled is the
  kill switch: no project is asked and no project's own setting is consulted,
  which is what makes it safe to shut the model down without visiting each
  project first.
- **status: on / off** - what a project that has said nothing about itself
  does. It is a default, and a project may state the opposite.

A project's own tab has the second one only. Left alone it reads
`inherited: on` or `inherited: off` with a dashed track - the position
resolved from above, which is not the same as this level having chosen it.
Touching it stores a choice; the **Inherit** button beside the row clears it
again.

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

An address stored in the settings is **the only one dialled**. Naming a server
is an instruction rather than a preference: falling through to another one
because the named server refused is how work quietly moves onto a CPU while
somebody watches an idle GPU and wonders why nothing arrives. With the field
empty the environment pair is used instead, and that one is a chain:

| Order | Address            | Set in                           |
| ----- | ------------------ | -------------------------------- |
| 1     | `EMBED_SERVER_URL` | `.env`, a machine with a GPU     |
| 2     | `EMBED_LOCAL_URL`  | `.env`, the `embedder` container |

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

A GPU is optional at every point. The `embedder` container runs llama.cpp on
CPU, and it is what answers when no address is stored and `EMBED_SERVER_URL`
is unset or silent.

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
A sweep in the loop does the same every few minutes, which is what fills a
project switched on long after it was last indexed.

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
  nothing would match every other nothing.
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
