---
layout: default
title: Summarization
nav_order: 5
---

## What a summary is, and why the model writes it

Every file node in the graph carries one sentence saying what the file is
for. That sentence is what `get_node_summary` returns and what
`search_code_nodes` lists, so it is the difference between an agent reading
one line and an agent opening the file.

Three producers write it, and `metadata.summary_source` records which:

| `summary_source` | Written by                               | Quality                  |
| ---------------- | ---------------------------------------- | ------------------------ |
| `auto`           | indexing, from the head of the file      | a docstring, or a guess  |
| `llm`            | a local GGUF model, in a pass of its own | a real description       |
| `manual`         | `save_node_summary`                      | yours, never overwritten |

`auto` is free and immediate: indexing takes the leading docstring,
comment block or first heading. It is also the reason the model pass exists.
A Terraform file with no header comment becomes "block: locals,
dynamic.alias, +2 more", which reads like a summary and answers nothing; a
205-line file under a thin `# Records` comment becomes "# Records". Both are
non-NULL, so nothing flags them - they simply make the graph less useful
than it looks.

The model pass replaces those with a sentence written from up to 16000
characters of the file. It costs seconds per file, which is why it is a
separate pass, and why most of this page is about where to run it.

## 1. On the stack itself, no worker

The simplest thing there is: nothing to install beyond the weights, nothing
to keep running.

### a. CPU

```bash
make llm-model-install                  # once: ~1 GB of weights
make summarize PROJECT=$(pwd)           # describe what has no model summary
make summarize PROJECT=$(pwd) BG=1      # the same, detached, for a large tree
make summarize PROJECT=$(pwd) LIMIT=20  # stop after 20, to time the rest
```

The model runs inside the `graphify` container, on whatever CPU the host
has, at roughly 8 seconds a file at 8 threads. `MODEL=` picks the weights,
for both the download and the run:

| `MODEL=`              | size    | ~s per file at 8 threads            |
| --------------------- | ------- | ----------------------------------- |
| `qwen-1.5b` (default) | 1.12 GB | ~8                                  |
| `qwen-3b`             | 2.10 GB | ~20, wants `GRAPHIFY_MEM=6g`        |
| `qwen-0.5b`           | 0.49 GB | ~4                                  |
| `smollm2`             | 1.06 GB | ~13, and it declines far more files |

The pass only visits files whose summary still comes from the head of the
file, so it can be stopped and restarted without repeating itself
(`FRESH=1` re-describes everything). Every answer is cached in
`summary_cache`, keyed by a hash of the text shown to the model, so a
re-index only pays for the files that changed.

Expect an occasional decline: a small model reading the head of a changelog
or a lock file can answer with just the file name, and an answer that says
no more than the node id is rejected rather than stored - those keep their
head-of-file summary. A `manual` summary is never touched by any of this.

### b. GPU, on that same machine

The container cannot use the card. Its `llama-cpp-python` is built with
`-DGGML_CUDA=OFF -DGGML_NATIVE=OFF` on purpose (`graphify/Dockerfile`), so
the image runs on any host CPU and on none of its GPUs.

Using the GPU therefore means one more process: `llama-server` holds the
model on the card, and a worker feeds it. Both can live on this same
machine, over loopback:

```bash
make up                                 # the API serves the queue at /worker

docker run --rm --gpus all -p 8080:8080 \
    -v ~/.local/share/enggraph/models:/models \
    ghcr.io/ggml-org/llama.cpp:server-cuda \
    -m /models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf \
    -c 8192 -ngl 99 --host 0.0.0.0 --port 8080 --parallel 1

cd worker && python3 -m enggraph_worker \
    --api http://127.0.0.1:3000/worker --token "$WORKER_API_TOKEN" \
    --project alpha --llama-server http://127.0.0.1:8080
```

The weights are the ones `make llm-model-install` already downloaded. The
worker in this mode loads no model of its own and needs nothing installed:
`enggraph_worker` is standard library apart from the model, so it runs from a bare
checkout. `WORKER_API_TOKEN` comes from the stack's `.env`
(`openssl rand -hex 24`), and the API refuses to start without it; `make up`
generates one when `.env` has none.

## 2. The worker on another machine

The same three pieces, spread out. The machine running the model needs no
checkout of the code it describes and no database access: the file text
reaches it over HTTP.

On the stack, once:

```bash
make up                              # generates WORKER_API_TOKEN if unset
make mounts                          # so the API can read every tree
make jobs PROJECT_NAME=alpha         # queue every file with no model summary
make jobs PROJECT_NAME=alpha         # queue every file with no model summary
make job ID=7                        # how far along it is
```

The text is read off the mount when a worker claims a file, not stored in
the graph: every indexed tree is mounted read-only at `/code/<project>` and
the API is the only service that opens it. `input_chars` on the job decides
how much of a file the model is shown, bounded by `LLM_INPUT_CHARS`.

Files that look like secrets (`.env`, `*.pem`, `*.tfvars`, `id_rsa*`) get a
node and a head-of-file summary like any other, but the API refuses to serve
their text - to a worker, to the dashboard, to anything. A file the graph
names and the mount does not hold is reported as `skipped` with `not on the
mount`: the graph is ahead of the tree, and a fresh index run settles it.

The queue is served at `/worker` on the stack's entry point, which is plain
HTTP on every interface, so it belongs on a trusted LAN or a VPN. Terminating
TLS is the entry point's job if it has to travel further. It refuses a token
shorter than 16 characters.

### a. Linux

**With `llama-server`, from the project's own image.** The llama.cpp
releases publish no Linux CUDA archive, so the image is the short path here,
and it is the same one as above:

```bash
docker run --rm --gpus all -p 8080:8080 \
    -v ~/.local/share/enggraph/models:/models \
    ghcr.io/ggml-org/llama.cpp:server-cuda \
    -m /models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf \
    -c 8192 -ngl 99 --host 0.0.0.0 --port 8080 --parallel 1

cd worker
python3 -m enggraph_worker.download          # the weights, if this machine has none
python3 -m enggraph_worker --api http://192.168.1.10:3000/worker --token <token> \
    --project alpha --llama-server http://127.0.0.1:8080
```

Without a GPU, `ghcr.io/ggml-org/llama.cpp:server` is the same image built
for the CPU, and `llama-b<tag>-bin-ubuntu-x64.tar.gz` from the releases is
the same server without Docker.

**With `llama-cpp-python` in the worker's own process**, no server:

```bash
cd worker
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
python -m enggraph_worker.download
python -m enggraph_worker --api http://192.168.1.10:3000/worker --token <token> \
    --project alpha
```

Drop the index and pass `--gpu-layers 0` on a machine with no card - but
PyPI's own `llama-cpp-python` ships no wheel, so a C/C++ toolchain
(`apt install build-essential`) has to be present to build it. `pip`
downloading a `.tar.gz` instead of a `.whl` is that happening.

### b. Windows

**With `llama-server`.** Three batch files, each of which can be
double-clicked or run from `cmd`:

```bat
cd worker
get-llama-server.bat
start-llama-server.bat
start-worker.bat --api http://192.168.1.10:3000/worker --token <token> --project alpha
```

`get-llama-server.bat` reads the current llama.cpp release, asks
`nvidia-smi` which CUDA the driver serves, and unpacks the matching build
together with its CUDA runtime into `worker\llama-server\`.
This is the chat server, and it is the one summarizing uses. Embedding needs
a second process with a different model - `start-llama-embeddings.bat`, port
8081 - because one `llama-server` serves one model. See
[embedding](https://oberon-systems.github.io/enggraph/embedding.html).

`start-llama-server.bat` starts it on the default weights and downloads
both the binaries and the weights first if they are missing - so on a clean
machine it is the only one of the three actually needed.
`start-worker.bat` runs the loop against `http://127.0.0.1:8080` unless
`WORKER_LLAMA_SERVER` says otherwise. All three pass extra arguments
straight through: `start-llama-server.bat --model qwen-3b --port 8090`.

In the server's log, `loaded CPU backend from ...ggml-cpu-<variant>.dll` is
the line that matters, and `offloaded 29/29 layers to GPU` says the card
holds the model. The server also serves a web UI on its port: ask it
anything there, and the model half is proven before the worker starts.

**With `llama-cpp-python` in the worker's own process:**

```bat
cd worker
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
pip install nvidia-cuda-runtime-cu12 nvidia-cublas-cu12
py -c "import glob,os,shutil,sysconfig;p=sysconfig.get_paths()['purelib'];[shutil.copy(f,os.path.join(p,'llama_cpp','lib')) for f in glob.glob(os.path.join(p,'nvidia','*','bin','*.dll'))]"
py -c "import llama_cpp; print('llama_cpp ok')"
py -m enggraph_worker.download
py -m enggraph_worker --api http://192.168.1.10:3000/worker --token <token> --project alpha
```

The `nvidia-` install and the copy after it are not optional: the wheel
carries `llama.dll` and `ggml-cuda.dll` but not the CUDA runtime they link
against, and `llama_cpp\lib\` is where the loader looks. Use the `cu124`
index - it is the only one of `abetlen`'s CUDA indexes currently publishing
Windows wheels for a recent release, and CUDA is driver-backward-compatible,
so it runs on a newer driver too.

This route can also fail in a way no flag fixes. A wheel is compiled for one
instruction set, and ggml settles which kernels exist at compile time, so a
wheel built with AVX-512 claims AVX-512 on every machine and dies with
`OSError: [WinError -1073741795] Windows Error 0xc000001d` - an illegal
instruction - on a CPU that has none. Every Ryzen before Zen 4 is such a
CPU. `llama-server` is what avoids it: the release binaries carry one
`ggml-cpu` library per instruction set and choose at load time. This stack's
own image sidesteps the same trap from the other side, by building with
`GGML_NATIVE=OFF`.

## 3. Pushed by the stack itself

The two ways above both need somebody to start them: a `make summarize` in a
terminal, or a worker process on the machine with the GPU. This one runs
unattended.

Put the address of a `llama-server` on the settings page - globally, or on one
project's settings tab - press **Test** so it answers before it is stored,
then **Save** with the switch on. From then on the worker API pushes the same
queue at that address on its own: a few files a batch, one batch a tick, for
as long as the server answers.

```text
Settings -> Summarizing

[ (o__) on ]  [ http://192.168.1.23:8080 ]  [ Test ]
```

A server reachable from outside its own machine is started with `--api-key`,
and that token goes in the field beside the URL. It is stored write-only,
sent as `Authorization: Bearer`, never shown again, and reported as `token
set, renew by <date>` - turning red after 30 days as a rotation reminder
rather than as a revocation.

It is the same queue and the same gates. Nothing here writes a summary the
pull path would not have written: the reply goes through the same shaping,
the same "says nothing the file name does not" rejection and the same
`summary_cache`, and a `manual` summary is never touched.

The **Queues** page in the dashboard shows how far this has got: files
described out of files there are, per project, beside the embedding queue, and
what each still owes. `GET /api/summaries` is the same thing as JSON.

Two things are worth knowing:

- **No address means no push.** The switch alone does nothing; a summary
  costs seconds of somebody's GPU, and a queue that pushed at an address
  nobody named would be spending it uninvited.
- **Two switches, not one.** Globally, enabled/disabled decides whether
  summarizing may happen at all, and status on/off is what a project that
  says nothing does. A project may state the opposite of that default; it
  cannot state the opposite of disabled.
- **A server that goes away costs the files nothing.** The batch is handed
  back with the attempt it spent returned, and the reason is logged once
  rather than once per file - so an afternoon with the GPU box off leaves the
  queue exactly as it was.

## 4. Which one to use

**Use `llama-server`.** It is the shorter setup on both platforms, it is the
only one that cannot be defeated by the CPU the wheel happened to be built
for, and it keeps the model out of the worker's process: the loop then needs
no compiler, no CUDA runtime and no wheel, and moves to another machine by
changing one URL. On Windows it is three batch files; on Linux it is a
`docker run`.

Take `llama-cpp-python` in-process only where a working wheel is already
installed, or where one more process is genuinely unwelcome.

And when none of this is set up, `make summarize` on the stack is always
there: slower per file, but nothing to install and nothing to keep running.

For a machine that is on anyway, prefer the push above to running the worker
loop: it is one URL on a settings page rather than a process to keep alive,
and it stops on its own when the switch goes off or the server goes away.

## Across the LAN

`--host 127.0.0.1` means only that machine can reach the server. To drive
one from another machine, three things change:

```bat
start-llama-server.bat --host 0.0.0.0 --api-key <secret>

netsh advfirewall firewall add rule name="llama-server" ^
  dir=in action=allow protocol=TCP localport=8080

start-worker.bat --api http://192.168.1.10:3000/worker --token <token> ^
  --project alpha --llama-server http://192.168.1.23:8080 ^
  --llama-server-key <secret>
```

`--api-key` is not optional once the port is open: `llama-server` has no
authentication of its own, and anything that reaches it can run the model
and read what is being described. Plain HTTP, exactly like the stack's own
queue - a trusted LAN or a VPN, and nowhere else.

## How it works

The queue is leased, not handed out: a worker claims a batch for five
minutes, and a batch whose worker dies returns to the queue for whoever asks
next. Several workers can share one job.

Answers are not trusted blindly. Each one goes through the same "says
nothing the file name doesn't" gate as the local pass, is length-capped, and
can never overwrite a `manual` summary. Already-summarized files are served
from `summary_cache` and never leased again.

The local pass and a remote job show the model different amounts of text
(2000 characters vs 16000), so they fill different cache rows - expected,
not a bug.

Full flag reference, the model catalogue and troubleshooting live in
[`worker/README.md`](https://github.com/oberon-systems/enggraph/blob/main/worker/README.md).
