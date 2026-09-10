"""Settings the indexer reads once at start up.

Everything here is a knob or a limit. Nothing in this module imports the rest
of the package, so it stays importable from anywhere.
"""

import os
from typing import NamedTuple

# Where a tree is mounted before it is a project at all. `enggraph.bootstrap`
# runs against a checkout with no row, no settled name and no generated mount,
# usually with the stack down, so it gets a fixed path of its own. Nothing
# else uses it.
SCAN_PATH = os.getenv("TARGET_PROJECT_PATH", "/project")
# Every indexed tree is mounted read-only at CODE_ROOT/<project> by the
# generated compose override, so a pass over several projects reads the files
# of all of them rather than the one that happened to be mounted.
CODE_ROOT = os.getenv("CODE_ROOT", "/code")
# The host path of the tree being indexed. Inside the container it is reached
# through the mount above, which says nothing about where it came from, so the
# real location is passed separately and recorded in the projects table. That
# is what lets a project name be traced back to a checkout, and what stops two
# checkouts sharing a basename from merging into one graph.
PROJECT_ROOT = os.getenv("PROJECT_ROOT", "")
# What the project is addressed by everywhere else: the `project` argument of
# the MCP tools, and the /mcp/<name> endpoint a client connects to. Derived
# from the last segment of PROJECT_ROOT when not set.
PROJECT_NAME = os.getenv("PROJECT_NAME", "")
# What kind of thing the project is, stored on the projects row and used by
# the MCP tools to narrow a cross-project search. Empty means "leave it as it
# is", so a re-index without TYPE= does not demote a project to the default.
PROJECT_TYPE = os.getenv("PROJECT_TYPE", "").strip() or None
# Documentation, not validation: the column is deliberately unconstrained, so
# an unknown value is warned about and then stored.
KNOWN_PROJECT_TYPES = frozenset(
    {
        "codebase",
        "docs",
        "config",
        # A project that is no tree of its own but a set of them: a monorepo in
        # slices, or a thematic keeper collecting projects so one search
        # reaches all of them. It reads named directories and never an
        # unnamed one, which is why it has no root path of its own.
        "organization",
        "memory",
        "plans",
        "suggestions",
        "settings",
    }
)
DEFAULT_PROJECT_TYPE = "codebase"
# The type that holds other projects. It reads no tree of its own: its members
# keep their names, their addresses and their graphs, and a search over the
# organization is a search over all of them.
ORGANIZATION_PROJECT_TYPE = "organization"
# The types that are not trees at all. A project of one of these holds
# records written through the MCP tools, so an index run would prune every
# one of them rather than refresh anything.
BUILTIN_PROJECT_TYPES = frozenset({"memory", "plans", "suggestions", "settings"})
# Reserved for built-in projects the indexer never writes: `_memory`,
# `_plans`, `_suggestions` and `_settings` today, `_common` when shared
# records land.
BUILTIN_NAME_PREFIX = "_"
# The built-in project holding the global selection defaults, as the row
# ('_settings', ''). Every project falls back to it.
SETTINGS_PROJECT = "_settings"
# The selection files, newest name first. The old pair is still read because
# it sits in trees this stack mounts read-only and cannot edit: dropping it
# would silently change what those projects index.
IGNORE_FILES = (".enggraph-ignore", ".ctxignore")
KEEP_FILES = (".enggraph-keep", ".ctxkeep")
IGNORE_FILE = IGNORE_FILES[0]
KEEP_FILE = KEEP_FILES[0]
# Re-extract every file instead of trusting either cache: the extractor's own
# per-file cache and our file_hashes table. For when a cache is suspected
# rather than known to be wrong. The API's `fresh` flag sets it.
FORCE_REEXTRACT = os.getenv("FORCE_REEXTRACT", "").strip().lower() not in {
    "",
    "0",
    "false",
    "no",
}

# Where a schedule lives in `project_settings.settings`, at any of the three
# levels the selection uses. An absent field asks the level above.
INDEXING_KEY = "indexing"
# What a schedule may say: manual only, a timer, or the mounts watched with
# the timer left as a fallback.
INDEXING_MODES = ("off", "periodic", "auto")
DEFAULT_INDEXING_MODE = "off"
DEFAULT_INDEX_INTERVAL_MINUTES = 60
DEFAULT_INDEX_DEBOUNCE_MINUTES = 5
# Clamped when read rather than refused: these values are editable in psql as
# well as in the dashboard, and one typed as 0 would spin the scheduler.
MIN_INDEX_INTERVAL_MINUTES = 1
MAX_INDEX_INTERVAL_MINUTES = 10080
MIN_INDEX_DEBOUNCE_MINUTES = 1
MAX_INDEX_DEBOUNCE_MINUTES = 1440
# Whether this process starts runs of its own. On by default, and the API is
# the only process it should ever be on in: the one-shot container and the
# suite import the same package.
SCHEDULER_ENABLED = os.getenv("INDEX_SCHEDULER", "").strip().lower() not in {
    "0",
    "false",
    "no",
}
SCHEDULER_TICK_SECONDS = int(os.getenv("SCHEDULER_TICK_SECONDS", "30"))
# How many runs one tick may start. Indexing is CPU-bound and a restart can
# leave every project overdue at once, so they are started a tick apart rather
# than all together.
SCHEDULER_STARTS_PER_TICK = int(os.getenv("SCHEDULER_STARTS_PER_TICK", "1"))

# The background features an operator can switch off, as keys of the same
# settings object the schedule lives in. `indexing` is the key the schedule
# already uses: the switch is another field of it rather than a second place
# to look.
FEATURE_INDEXING = INDEXING_KEY
FEATURE_SUMMARIZE = "summarize"
FEATURE_EMBEDDING = "embedding"
# What a feature does where no level says anything. Indexing and summarizing
# stay on, so the switch changes nothing until it is used: the schedule goes
# on deciding whether a project is indexed without being asked, and a summary
# job is still opened by hand. Embedding starts off - it is a queue that would
# otherwise begin filling itself on a fresh install, before anyone chose to
# run a model at all.
FEATURE_FIELD_DEFAULTS: dict[str, bool | str | int] = {
    # Allowed unless somebody says otherwise: this is the kill switch, and a
    # stack that never touches it behaves as it always did.
    "allowed": True,
    "server_url": "",
    "server_key": "",
    "key_saved_at": "",
}
# The pace each queue keeps where nobody has said otherwise. Indexing has no
# queue of its own, so its numbers are there to keep one shape for all three
# and are read by nothing.
FEATURE_DEFAULTS: dict[str, dict[str, bool | str | int]] = {
    FEATURE_INDEXING: {
        "enabled": True,
        "batch": 1,
        "tick_seconds": 30,
        "budget_seconds": 60,
        "chunk_chars": 1500,
        "chunk_overlap": 5,
        **FEATURE_FIELD_DEFAULTS,
    },
    FEATURE_SUMMARIZE: {
        "enabled": True,
        "batch": 4,
        "tick_seconds": 30,
        "budget_seconds": 120,
        "chunk_chars": 1500,
        "chunk_overlap": 5,
        **FEATURE_FIELD_DEFAULTS,
    },
    FEATURE_EMBEDDING: {
        "enabled": False,
        "batch": 8,
        "tick_seconds": 10,
        "budget_seconds": 60,
        # The window a file is cut into, and how many lines two windows share
        # so a declaration on a boundary is whole in one of them.
        "chunk_chars": 1500,
        "chunk_overlap": 5,
        **FEATURE_FIELD_DEFAULTS,
    },
}
# How long a stored server token is good for before the dashboard asks for a
# new one. Advisory: the key goes on being sent past it, and the line beside
# the field turns red. A server that has actually revoked the key is the one
# that says so, with a 401.
FEATURE_KEY_TTL_DAYS = int(os.getenv("FEATURE_KEY_TTL_DAYS", "30"))

DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".cache",
        ".git",
        ".gradle",
        ".idea",
        ".mypy_cache",
        ".next",
        ".pre-commit",
        ".pytest_cache",
        ".ruff_cache",
        ".terraform",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "pgdata",
        "target",
        "vendor",
        "venv",
    }
)

# Generated files that parse as a supported format but say nothing their
# source file does not. Skipped when the project has no .enggraph-keep; an
# explicit .enggraph-keep still wins, since that is the project asking for them
# by name.
IGNORED_FILE_NAMES = frozenset(
    {"composer.lock", "npm-shrinkwrap.json", "package-lock.json", "yarn.lock"}
)

# graph_nodes.id and graph_nodes.name are VARCHAR(255). Longer values are
# truncated here so one deep path cannot abort the transaction.
MAX_NODE_ID_LENGTH = 255
MAX_NAME_LENGTH = 255
# graph_nodes.type is VARCHAR(50), and every parser is free to name an entity
# kind of its own.
MAX_TYPE_LENGTH = 50
# projects.name is VARCHAR(64). It also travels in a URL path, so the
# characters it may contain are narrower than the column would allow.
MAX_PROJECT_NAME_LENGTH = 64
# Generated files (minified bundles, vendored blobs) cost minutes of parsing
# and contribute nothing but noise.
MAX_FILE_BYTES = 1_000_000
# Separates the owning file from the entity name in a node id, so two files
# may each define `main` without collapsing into one node.
ENTITY_SEPARATOR = "::"
# Extensions worth a file node even though no parser looks inside them.
EXTRA_SOURCE_EXTENSIONS = (".sql",)
# Extensions handed to the graphifyy extractor instead of a parser of our own.
# It covers more languages than we do and tags every edge with a confidence,
# so code goes to it and the infrastructure formats it cannot read stay here.
GRAPHIFYY_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".kt",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".ts",
        ".tsx",
    }
)
# Where the graph is materialized for the tools that read a file rather than
# the database. graphifyy's own security check refuses anything outside a
# `graphify-out` directory next to the working directory, so the name is not
# ours to choose.
GRAPHIFY_OUT_DIR = os.getenv("GRAPHIFY_OUT_DIR", "graphify-out")
# Marks which producer wrote a node or an edge, so re-running one of them
# never clears what the other found.
SOURCE_NATIVE = "native"
SOURCE_GRAPHIFYY = "graphifyy"
# Suffixes tried when resolving a path-like import to an indexed file.
MODULE_EXTENSIONS = (
    ".ts",
    ".tsx",
    ".d.ts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".go",
    ".rs",
)
# Suffixes a TypeScript import may carry while naming a source file that has
# a different one ("./util.js" resolving to "util.ts").
REWRITABLE_IMPORT_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs")
# Comment markers stripped when a summary is taken from the head of a file.
COMMENT_MARKERS = ('"""', "'''", "###", "#", "//", "/*", "*/", "*", "--", "<!--")
SUMMARY_SCAN_LINES = 40
MAX_SUMMARY_LENGTH = 300
# How many declared names a fallback summary lists before it says "+N more".
SUMMARY_ENTITY_LIMIT = 8

# Whether an index run also writes model summaries. Off by default: a first
# index of a large tree would spend hours in the model, and the summaries the
# parsers write are there immediately. The API's `summarize` flag turns it on
# for one run, and `make summarize` does the same work afterwards instead.
SUMMARIZE = os.getenv("SUMMARIZE", "").strip().lower() not in {
    "",
    "0",
    "false",
    "no",
}
# Which GGUF weights to load. `make llm-model-install` puts them under
# ~/.local/share/enggraph/models on the host, mounted read-only at
# LLM_MODEL_DIR, and every `make` target that runs the model names the file it
# downloaded. Left empty, the directory is searched instead, which is what
# keeps a hand-rolled `docker run` working.
LLM_MODEL_DIR = os.getenv("LLM_MODEL_DIR", "/models")
LLM_MODEL_PATH = os.getenv("LLM_MODEL_PATH", "").strip()
# Kept at or below the cpu quota the container runs under: llama.cpp threads
# oversubscribed against a quota buy context switches, not throughput.
LLM_THREADS = int(os.getenv("LLM_THREADS", "2"))
LLM_CTX = int(os.getenv("LLM_CTX", "2048"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "64"))
# The prompt is clamped to this many characters, so a file can never overflow
# the context window. It is also the setting that decides how long the pass
# takes: reading the prompt is most of the work on a CPU, and the answer is
# 64 tokens whatever the input was. About 25 lines of code, against the 40
# `extract_summary` reads for the same purpose.
LLM_INPUT_CHARS = int(os.getenv("LLM_INPUT_CHARS", "2000"))
# Stop the summarizing pass after this many files. Zero is all of them. It is
# how a first pass over a large tree is timed before one is spent on it.
SUMMARY_LIMIT = int(os.getenv("SUMMARY_LIMIT", "") or 0)

# Where a file is described, when the queue is pushed rather than pulled.
# Unset, nothing is pushed: `make summarize` in this image and a worker
# claiming leases from the API are the two ways the queue was always drained,
# and both go on working. A URL stored in the settings comes before this.
SUMMARIZE_SERVER_URL = os.getenv("SUMMARIZE_SERVER_URL", "").strip().rstrip("/")
SUMMARIZE_SERVER_KEY = os.getenv("SUMMARIZE_SERVER_KEY", "").strip()
# How often the push loop looks, and how many files one batch takes. A file
# costs seconds of somebody's GPU, so the batch is small and the tick is slow.
SUMMARIZE_TICK_SECONDS = int(os.getenv("SUMMARIZE_TICK_SECONDS", "30"))
SUMMARIZE_BATCH = int(os.getenv("SUMMARIZE_BATCH", "4"))
# How long a server that did not answer is left alone before it is dialled
# again, as with the embedder.
SUMMARIZE_PROBE_SECONDS = int(os.getenv("SUMMARIZE_PROBE_SECONDS", "60"))
# Whether this process pushes the summary queue. On by default and true only
# in the worker API, for the reason SCHEDULER_ENABLED gives.
SUMMARIZE_LOOP_ENABLED = os.getenv("SUMMARIZE_LOOP", "").strip().lower() not in {
    "0",
    "false",
    "no",
}

# The embedding model, and where it runs. Both URLs speak the OpenAI
# embeddings route llama-server implements, so which one answers is a matter
# of configuration rather than of code: the first is the machine with the GPU
# and the second the small model this stack can run beside itself. A URL
# stored in the settings comes before both, which is how the dashboard points
# a project somewhere else.
EMBED_SERVER_URL = os.getenv("EMBED_SERVER_URL", "").strip().rstrip("/")
EMBED_LOCAL_URL = os.getenv("EMBED_LOCAL_URL", "").strip().rstrip("/")
# The token a published server wants. The dashboard stores one per level and
# that comes first; this is the fallback for a stack configured by file alone.
EMBED_SERVER_KEY = os.getenv("EMBED_SERVER_KEY", "").strip()
EMBED_PATH = "/v1/embeddings"
# Named in the request and recorded on every row. Two models never share a
# vector space, so a row written by another one is stale however fresh the
# file behind it is.
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text-v1.5")
# What the column is declared as. A model of another width needs a migration,
# not a variable, so this is here to be checked against rather than to be set.
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
# How a file is cut up. The window is characters rather than tokens because
# nothing here has a tokenizer, and the overlap is lines so a declaration split
# across a boundary is whole in one of the two chunks.
EMBED_CHUNK_CHARS = int(os.getenv("EMBED_CHUNK_CHARS", "1500"))
EMBED_CHUNK_OVERLAP_LINES = int(os.getenv("EMBED_CHUNK_OVERLAP_LINES", "5"))
# How many chunks travel in one request, and how many files one claim takes.
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "8"))
EMBED_TASKS_PER_TICK = int(os.getenv("EMBED_TASKS_PER_TICK", "8"))
# How long the loop sleeps when there was nothing to do. It is a poll
# interval, not a throttle: while the queue has work the loop keeps claiming
# rather than doing a handful and sleeping, which is what made a fast server
# look idle between one four-file batch and the next.
EMBED_TICK_SECONDS = int(os.getenv("EMBED_TICK_SECONDS", "10"))
# How long one tick may keep working before it goes back and reads the
# switches again. The switch has to take effect promptly, and a tick that
# drained a large project end to end would not notice it for hours.
EMBED_TICK_BUDGET_SECONDS = int(os.getenv("EMBED_TICK_BUDGET_SECONDS", "60"))
# How long a claimed file is held before it returns to the queue, and how many
# times a file that kills the loop is handed out before it is left alone.
EMBED_LEASE_SECONDS = int(os.getenv("EMBED_LEASE_SECONDS", "300"))
EMBED_MAX_ATTEMPTS = int(os.getenv("EMBED_MAX_ATTEMPTS", "3"))
# How long a server that did not answer is left alone before it is dialled
# again. Without it a queue with nowhere to go would connect once per file.
EMBED_PROBE_SECONDS = int(os.getenv("EMBED_PROBE_SECONDS", "60"))


class Timeouts(NamedTuple):
    """Seconds allowed per phase of one model request, and for all of it."""

    connect: float
    write: float
    read: float
    total: float


# A queue batch halves itself on a read timeout, so read stays generous; a
# query must answer inside the MCP server's 5 s, local fallback included.
EMBED_QUEUE_TIMEOUTS = Timeouts(connect=1, write=5, read=120, total=180)
EMBED_QUERY_TIMEOUTS = Timeouts(connect=1, write=2, read=3, total=4)
CHAT_TIMEOUTS = Timeouts(connect=1, write=5, read=240, total=300)
PROBE_TIMEOUTS = Timeouts(connect=1, write=5, read=10, total=15)
# Whether this process drains the embedding queue. On by default and true only
# in the worker API, for the reason SCHEDULER_ENABLED gives: the one-shot
# container and the suite import the same package.
EMBED_LOOP_ENABLED = os.getenv("EMBED_LOOP", "").strip().lower() not in {
    "0",
    "false",
    "no",
}

# Files that get a node and a head-of-file summary like any other, but whose
# text the API never serves. A tree without a .enggraph-ignore is the case this
# exists for: the mount holds whatever the checkout holds, and a key in it
# must not travel over the network because a node names the file.
CONTENT_DENIED_NAMES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jwt",
    "*.tfvars",
    ".htpasswd",
    "authorized_keys",
    "credentials",
    "id_rsa*",
    "id_ed25519*",
)

# The worker API. It is the one service here that is meant to be reached from
# another machine, so the token is not optional: it refuses to start without
# one, and a short one is a configuration mistake rather than a choice.
WORKER_API_PORT = int(os.getenv("WORKER_API_PORT", "3003"))
WORKER_API_TOKEN = os.getenv("WORKER_API_TOKEN", "")
WORKER_API_TOKEN_MIN = 16
# FastAPI cannot put its own docs page behind the token, and an open route on
# a published port is the thing being avoided.
WORKER_API_DOCS = bool(os.getenv("WORKER_API_DOCS", ""))
# How long a claimed batch is held before it returns to the queue, and how
# many files one claim may take.
WORKER_LEASE_SECONDS = int(os.getenv("WORKER_LEASE_SECONDS", "300"))
WORKER_MAX_BATCH = int(os.getenv("WORKER_MAX_BATCH", "8"))
# A file that reliably kills the worker is dropped rather than retried
# forever.
WORKER_MAX_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS", "3"))
# A reply longer than this is refused before it is shaped: the worker is a
# network peer, and the summary it is answering with is one sentence.
WORKER_MAX_REPLY_CHARS = int(os.getenv("WORKER_MAX_REPLY_CHARS", "4000"))
