# Developer entry points for enggraph. Run `make` for the target list.

NAME := enggraph
VENV ?= .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip

VERSION := $(shell sed -n 's/^  version: \(.*\)/\1/p' .cz.yaml)

COMPOSE ?= docker compose
DOCKER ?= docker

# Inherited by the service Makefiles the same way TAG is: each one repeats the
# default so it still builds standalone, and a value set here wins. The images
# are named for the registry they are published to, so a local build replaces
# the reference docker-compose.yaml pins instead of producing one nothing runs.
REGISTRY ?= ghcr.io
NAMESPACE ?= oberon-systems/enggraph
TAG ?= latest
export TAG

# One image per Python service, each built from packages/<name>/Dockerfile
# with the repository root as its context.
PY_IMAGES := api embed summarize viewer
PY_REFS := $(foreach one,$(PY_IMAGES),$(REGISTRY)/$(NAMESPACE)/$(one):$(TAG))
MCP_IMAGE := $(REGISTRY)/$(NAMESPACE)/mcp-server
WEB_IMAGE := $(REGISTRY)/$(NAMESPACE)/web

MCP_DIR := mcp-server
WEB_DIR := web
MIGRATIONS_DIR := packages/core

# One CPU and memory budget for the whole stack (STACK_CPUS, STACK_MEM), split
# into per-service limits every compose call below is started with.
$(foreach limit,$(shell COMPOSE='$(COMPOSE)' scripts/limits.sh),$(eval export $(limit)))

# Process substitution in the shell target needs bash, not sh.
SHELL := /bin/bash

.DEFAULT_GOAL := help

# `make mcp build` reads as a subcommand, but make sees two goals. Absorb
# everything after the subdivision name into do-nothing rules so only the
# delegation runs. Root target names are left alone, otherwise make warns about
# the override.
SUBS := mcp db web
ROOT_GOALS := help init install reregister shell lint check build pull up down \
	restart logs ps status mounts limits \
	backup restore psql clean build-py \
	llm-model-install api-logs jobs job eval eval-up eval-down eval-checks \
	eval-baseline replay \
	test test-mcp test-py test-eval $(SUBS)
ifneq (,$(filter $(firstword $(MAKECMDGOALS)),$(SUBS)))
SUBARGS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
$(eval $(filter-out $(ROOT_GOALS),$(SUBARGS)):;@:)
endif

.PHONY: help init install reregister mounts limits \
	shell lint check build pull up down restart logs ps \
	status backup restore psql clean build-py \
	mcp db web \
	llm-model-install api-logs jobs job eval eval-up eval-down eval-checks \
	replay \
	test test-mcp test-py test-eval \
	require-venv require-env require-not-root require-model

help:  ## Show the current version and the available targets
	@echo "$(NAME) $(VERSION)"
	@echo
	@echo "Targets:"
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-10s %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)
	@echo
	@echo "  mcp <target>"
	@echo
	@$(MAKE) --no-print-directory -C $(MCP_DIR) \
		IMAGE='$(MCP_IMAGE)' help | sed 's/^\(.\)/      \1/'
	@echo
	@echo "  web <target>"
	@echo
	@$(MAKE) --no-print-directory -C $(WEB_DIR) \
		IMAGE='$(WEB_IMAGE)' help | sed 's/^\(.\)/      \1/'
	@echo
	@echo "  db <target>"
	@echo
	@$(MAKE) --no-print-directory -C $(MIGRATIONS_DIR) \
		help | sed 's/^\(.\)/      \1/'
	@echo

init:  ## Create the virtualenv and install the pre-commit hooks
	python3 -m venv --prompt $(NAME) $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install pre-commit commitizen ruff 'penpot-local-stack==0.1.0'
	# The commit adapter is optional. Where it cannot be installed, commitizen
	# falls back to its own rules and .cz.yaml has to stop naming this one.
	-$(PIP) install 'wyld-cz>=0.4.1'
	# Every package editable, core first: the others name it as a dependency.
	$(PIP) install -r requirements-dev.txt \
		$(foreach one,core indexer api embed summarize viewer,-e packages/$(one))
	$(VENV)/bin/pre-commit install --install-hooks
	@test -f .env || cp .env.example .env
	# The eslint/tsc hooks run from each tree's own node_modules, so
	# `make lint` needs both present.
	@command -v npm > /dev/null \
		&& $(MAKE) --no-print-directory -C $(MCP_DIR) deps \
		&& $(MAKE) --no-print-directory -C $(WEB_DIR) deps \
		|| echo "npm not found, run 'make mcp deps' and 'make web deps' first"
	@echo "Initialization complete. Edit .env, then run 'make build && make up'."

# Everything a codebase needs to be usable from an agent, in one pass: the
# `enggraph` server registered for both agents, an instruction file, the
# selection generated from what the tree actually holds and stored on the
# project row, and the projects row the rest of the stack addresses the tree
# by. Nothing is written into the tree being onboarded except the agent files
# themselves; the skills arrive from the MCP server.
#
# AGENT_ROOT is the codebase being onboarded; it defaults to the directory make
# was called from, so `make -C` here from inside a codebase onboards it (-C
# moves CURDIR but leaves PWD naming the caller). Nothing it writes replaces a
# file that exists, which is what makes a second run safe: it fills in
# whatever is missing and reports the rest as kept.
#
# TYPE= categorises the project for the cross-project search - codebase, docs
# or config - and is stored with the row; empty keeps whatever a project was
# registered as before. SHELL_RC= names the rc file the shell aliases earlier
# versions wrote are taken out of. PERMISSIONS=0 leaves
# ~/.claude/settings.json alone,
# at the price of a confirmation prompt on every call the agent makes.
# MAKE_PREFIX is how the onboarded codebase reaches these targets, substituted
# into the instruction file: a plain `make` from inside this repository, and
# `make -C` here from anywhere else, since the stack lives here and nothing of
# it is in the other repository. A codebase reaching this through a proxy
# target of its own passes that instead.
AGENT_ROOT ?= $(or $(realpath $(PWD)),$(CURDIR))
MAKE_PREFIX ?= $(if $(filter $(abspath $(AGENT_ROOT)),$(CURDIR)),make,make -C $(CURDIR))
SHELL_RC ?=
PERMISSIONS ?=

# Which tree the onboarded project reads, which is AGENT_ROOT itself unless
# it is said otherwise. SOURCE=none registers the project without a tree at
# all: the row, the agent files and the address exist, and what it holds is
# other projects - which is what an organization is.
SOURCE ?= $(AGENT_ROOT)
EMPTY = $(filter none,$(SOURCE))

# The one command a codebase needs. It stores the selection, writes the alias,
# registers the project and gives the API a mount for the tree - but it does
# not index. That is a button in the dashboard,
# because an index run is not part of setting up.
#
# Registering is the mount step: REGISTER=1 has it store the tree as a project
# with no indexed_at, which is what puts the row in the dashboard with an
# Index button next to it and what keeps the mount from being dropped the next
# time this override is generated from the table.
# FORCE=1 turns a re-run into a refresh: every file this version would write
# and the tree already has is shown as a diff and asked about, one at a time.
install: require-env require-not-root  ## Onboard AGENT_ROOT (SOURCE=none leaves it empty, TYPE= categorises it)
	@AGENT_ROOT='$(abspath $(AGENT_ROOT))' PROJECT_NAME='$(PROJECT_NAME)' \
		MAKE_PREFIX='$(MAKE_PREFIX)' MAKE_BIN='$(MAKE)' \
		COMPOSE='$(COMPOSE)' SHELL_RC='$(SHELL_RC)' \
		PERMISSIONS='$(PERMISSIONS)' FORCE='$(FORCE)' \
		SOURCE='$(if $(EMPTY),none,$(abspath $(SOURCE)))' \
		TYPE='$(TYPE)' scripts/install.sh
	@# The API is long lived, so a tree added just now is invisible to it
	@# until the container is recreated against the rewritten override.
	$(COMPOSE) up -d worker-api embed summarize

# Onboarding writes each codebase's agent files once, so a codebase onboarded
# before this project was renamed still addresses the server as `context`.
# This runs the registration step alone over every project that reads a whole
# tree, which moves that entry under the new key.
reregister: require-env  ## Rewrite every onboarded codebase's agent configuration
	@COMPOSE='$(COMPOSE)' scripts/reregister.sh

shell: require-venv  ## Open an interactive subshell with the virtualenv activated
	@$(SHELL) --rcfile <(cat ~/.bashrc 2> /dev/null; \
		echo 'source $(CURDIR)/$(VENV)/bin/activate') -i || true

lint: require-venv  ## Run the pre-commit hooks over every file
	$(VENV)/bin/pre-commit run --all-files

check: lint  ## Alias for lint

# Both services build under the reference compose runs, so a build replaces
# what the stack starts. That is worth saying out loud: the summary is how you
# see the image id actually moved, and a running stack still holds the previous
# one until it is recreated.
build: build-py  ## Build every service image
	@$(MAKE) --no-print-directory -C $(MCP_DIR) \
		IMAGE='$(MCP_IMAGE)' TAG='$(TAG)' build
	@$(MAKE) --no-print-directory -C $(WEB_DIR) \
		IMAGE='$(WEB_IMAGE)' TAG='$(TAG)' build
	@for ref in $(PY_REFS) $(MCP_IMAGE):$(TAG) $(WEB_IMAGE):$(TAG); do \
		$(DOCKER) image ls --format \
			'{{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}' "$$ref" \
			| sed 's/^/  /'; \
	done
	@test -z "$$($(COMPOSE) ps -q 2> /dev/null)" \
		|| echo "  stack is running, 'make up' recreates it with these"

build-py:
	@for one in $(PY_IMAGES); do \
		$(DOCKER) build -f packages/$$one/Dockerfile \
			-t $(REGISTRY)/$(NAMESPACE)/$$one:$(TAG) . || exit 1; \
	done

# The other end of `build`, and the reason it needs one: a local build takes
# over the same :latest reference the registry publishes, and `up` never
# re-pulls an image that is already present. This is what puts the published
# one back - and what a first run uses instead of building at all.
pull:  ## Pull the published images, discarding a local build
	$(COMPOSE) --profile index pull

# The viewer is named here rather than left to a bare `up` so that /graph,
# which the dashboard proxies, answers on a stack this target started.
#
# An override written before the two queues were services names no mounts for
# them, and they would read no tree; it is rewritten once, here.
up: require-env  ## Start the database, the services, the embedder and the entry point
	@if [ -f docker-compose.override.yaml ] \
			&& ! grep -q '^  embed:' docker-compose.override.yaml; then \
		$(MAKE) --no-print-directory mounts; \
	fi
	$(COMPOSE) up -d postgres valkey embedder worker-api embed summarize \
		mcp-server viewer web nginx

limits:  ## Show the CPU and memory each service gets from STACK_CPUS and STACK_MEM
	@COMPOSE='$(COMPOSE)' scripts/limits.sh

# The index job sits behind a profile, so a plain `down` does not see it: a
# graphify container left over from a run keeps the network alive and
# the teardown ends in "Resource is still in use". Name the profile so the
# whole project goes.
down:  ## Stop the stack, keeping the database volume
	$(COMPOSE) --profile index down --remove-orphans

restart: down up  ## Recreate the running services

# The token is read from .env and handed to curl on stdin rather than as an
# argument: an argument is visible in `ps` to every user of this machine.
GATEWAY_PORT ?= $(shell sed -n 's/^GATEWAY_PORT=//p' .env 2>/dev/null)
GATEWAY_PORT := $(or $(GATEWAY_PORT),3000)
API_URL := http://127.0.0.1:$(GATEWAY_PORT)/worker
CURL_AUTH = printf 'header = "Authorization: Bearer %s"\n' \
	"$$(sed -n 's/^WORKER_API_TOKEN=//p' .env)" | curl -sS --config -

api-logs:  ## Follow the API log
	$(COMPOSE) logs -f worker-api

jobs: require-env  ## Queue a summarization job: make jobs PROJECT_NAME=alpha
	@test -n '$(PROJECT_NAME)' || { \
		echo "PROJECT_NAME= is required" >&2; exit 1; \
	}
	@$(CURL_AUTH) -X POST '$(API_URL)/jobs' \
		-H 'Content-Type: application/json' \
		-d '{"project":"$(PROJECT_NAME)","refresh":$(if $(FRESH),true,false)}'
	@echo

job: require-env  ## Show a summarization job: make job ID=7
	@test -n '$(ID)' || { echo "ID= is required" >&2; exit 1; }
	@$(CURL_AUTH) '$(API_URL)/jobs/$(ID)'
	@echo

logs:  ## Follow the logs of the running services
	$(COMPOSE) logs -f

ps:  ## Show the state of every service
	$(COMPOSE) ps

# Answers two questions `ps` cannot: is the server reachable, and is anything
# actually using it. `sessions` in the health payload counts connected MCP
# clients, so zero there means no client attached however healthy the containers
# look. Every step is best-effort: a stopped stack reports what is missing
# rather than failing the target.
#
# GATEWAY_PORT is read out of .env rather than from `$(COMPOSE) port`, which
# only answers while the container runs and would leave the address
# unprintable in exactly the case worth reporting.
#
# The service list goes through xargs rather than `tr`: compose prints a bare
# newline when nothing runs, and a translated newline is a space, which is not
# empty enough for the shell default to fire.
status: require-env  ## Show whether the stack runs and whether anything uses it
	@running=$$($(COMPOSE) ps --services --filter status=running 2> /dev/null \
		| xargs); \
	echo "containers: $${running:-none running}"; \
	port=$$(sed -n 's/^GATEWAY_PORT=//p' .env | tail -1); \
	port=$${port:-3000}; \
	health=$$(curl -sS localhost:$$port/health 2> /dev/null); \
	echo "health:     $${health:-unreachable on localhost:$$port}"; \
	dash=$$(curl -sS localhost:$$port/api/health 2> /dev/null); \
	echo "dashboard:  $${dash:-unreachable on localhost:$$port/api}"; \
	graph=$$($(COMPOSE) exec -T postgres psql -U "$${POSTGRES_USER:-user}" \
		-d "$${POSTGRES_DB:-context}" -tAc "select string_agg( \
			p.name || ' (' || c.nodes || ')', ', ' order by p.name) \
			from projects p, lateral ( \
				select count(*) as nodes from nodes g \
				 where g.project = p.name) c" \
		2> /dev/null | tr -d '\r'); \
	echo "graph:      $${graph:-unavailable}"; \
	schema=$$($(COMPOSE) exec -T postgres psql -U "$${POSTGRES_USER:-user}" \
		-d "$${POSTGRES_DB:-context}" -tAc "select version_num \
			from alembic_version" 2> /dev/null | tr -d '\r'); \
	echo "schema:     $${schema:-unmigrated}"

BACKUP_DIR ?=
INDEXED := $(if $(PROJECT),$(abspath $(PROJECT)),)

# Every indexed tree is mounted read-only at /code/<project>, so a pass over
# several projects can read the files of all of them. The list is generated
# from the projects table rather than written by hand, which is what stops it
# drifting from what is actually indexed.
mounts: require-env  ## Rewrite docker-compose.override.yaml from the projects table
	@COMPOSE='$(COMPOSE)' ADD='$(INDEXED)' PROJECT_NAME='$(PROJECT_NAME)' \
		REGISTER='$(REGISTER)' CREATE='$(CREATE)' PROJECT_TYPE='$(TYPE)' \
		scripts/mounts.sh

# The weights the embedder runs. Mounted read-only at /models by compose, so
# MODEL_DIR is the host half of that mount and MODEL_NAME is the same file name
# on both sides - which is what stops the download and the container disagreeing
# about which model is in use.
#
# MODEL= picks one of the names below. The upstream file name is kept as it is,
# so several can sit in the directory at once.
MODEL ?= qwen-1.5b
# The table of models lives in worker/enggraph_worker/catalogue.py, which is also
# what the machine with the GPU reads: two copies would drift, and the Windows
# half cannot run this Makefile.
# Any interpreter answers: the catalogue is a table and imports nothing. The
# venv is preferred only so a machine without a system python3 still works.
CATALOGUE_PYTHON := $(if $(wildcard $(PYTHON)),$(PYTHON),python3)
CATALOGUE := PYTHONPATH='$(CURDIR)/worker' $(CATALOGUE_PYTHON) -m enggraph_worker.catalogue
MODELS := $(shell $(CATALOGUE) list 2>/dev/null)
MODEL_NAME := $(shell $(CATALOGUE) file '$(MODEL)' 2>/dev/null)
MODEL_DIR := $(HOME)/.local/share/enggraph/models
MODEL_FILE := $(MODEL_DIR)/$(MODEL_NAME)

# Downloaded beside the target name and moved into place only once it is a
# GGUF file: without `-f` curl saves the error page under the model's name and
# exits 0, and llama.cpp is then the one to report it, a run later.
llm-model-install: require-model  ## Download model weights (MODEL=, FORCE=1)
	@PYTHONPATH='$(CURDIR)/worker' $(CATALOGUE_PYTHON) -m enggraph_worker.download \
		--model '$(MODEL)' --dir '$(MODEL_DIR)' $(if $(FORCE),--force)

require-model:
	@test -n '$(MODEL_NAME)' || { \
		echo "unknown MODEL=$(MODEL), pick one of: $(MODELS)" >&2; \
		exit 1; \
	}

# The other maintenance pair: `backup` writes, `restore` puts back. Both take
# the same selectors the dashboard drops by - nothing named means everything -
# and both leave the destination directory to the script, which defaults it
# next to the database rather than reading a setting for it.
#
# FILE= names one file instead of the generated name, and then rotation leaves
# it alone: KEEP=N only ever prunes files this naming scheme produced, and each
# kind on its own.
backup: require-env  ## Back up the database, or PROJECT= / PROJECT_NAME= alone
	@COMPOSE='$(COMPOSE)' BACKUP_PATH='$(INDEXED)' \
		BACKUP_NAME='$(PROJECT_NAME)' BACKUP_FILE='$(FILE)' \
		BACKUP_DIR='$(BACKUP_DIR)' KEEP='$(KEEP)' scripts/backup.sh

# No default here, for the reason a drop never has one: a bare invocation
# that replaces a database is the accident worth designing out.
restore: require-env  ## Restore FILE= over the database or over one project
	@COMPOSE='$(COMPOSE)' BACKUP_FILE='$(FILE)' BACKUP_DIR='$(BACKUP_DIR)' \
		FORCE='$(FORCE)' scripts/restore.sh

psql: require-env  ## Open a psql session against the context database
	$(COMPOSE) exec postgres psql -U "$${POSTGRES_USER:-user}" -d "$${POSTGRES_DB:-context}"

# The database lives in a bind mount, not a volume, so `down -v` never reached
# it: that flag only drops named and anonymous volumes. Emptying the directory
# here is what makes the next `up` a fresh database - the migrate service
# rebuilds the schema into it - and what stops a regenerated
# POSTGRES_PASSWORD from meeting a database that still
# holds the old one and refuses every connection.
#
# The files belong to the postgres uid, so the host user cannot remove them.
# Compose runs the deletion as root inside the postgres service itself, which
# also means the path stays resolved by compose rather than parsed again here.
clean: require-env  ## Remove the containers, the database and the built images
	@test -n "$(FORCE)" || { \
		echo "This empties the database, and the index goes with it."; \
		read -r -p "Continue? [y/N] " reply; \
		case "$$reply" in [yY]*) ;; *) echo "aborted"; exit 1 ;; esac; \
	}
	$(COMPOSE) --profile index down --remove-orphans
	$(COMPOSE) run --rm --no-deps --user root --entrypoint sh postgres -c \
		'rm -rf /var/lib/postgresql/data/..?* \
			/var/lib/postgresql/data/.[!.]* \
			/var/lib/postgresql/data/*'
	$(COMPOSE) --profile index down -v --remove-orphans
	-$(DOCKER) image rm $(PY_REFS)
	@$(MAKE) --no-print-directory -C $(MCP_DIR) \
		IMAGE='$(MCP_IMAGE)' TAG='$(TAG)' clean
	@$(MAKE) --no-print-directory -C $(WEB_DIR) \
		IMAGE='$(WEB_IMAGE)' TAG='$(TAG)' clean

# The sub-Makefiles own their own target lists, so everything after the
# subdivision name is passed straight through: `make mcp build`, `make mcp`.
mcp:
	@$(MAKE) --no-print-directory -C $(MCP_DIR) \
		IMAGE='$(MCP_IMAGE)' TAG='$(TAG)' $(SUBARGS)

web:
	@$(MAKE) --no-print-directory -C $(WEB_DIR) \
		IMAGE='$(WEB_IMAGE)' TAG='$(TAG)' $(SUBARGS)

db:
	@$(MAKE) --no-print-directory -C $(MIGRATIONS_DIR) \
		COMPOSE='$(COMPOSE) -f $(CURDIR)/docker-compose.yaml' $(SUBARGS)

# The evaluation harness runs against a stack of its own: a tmpfs database on
# EVAL_DB_PORT and an MCP server on EVAL_MCP_PORT, both on loopback.
EVAL_DB_PORT ?= 55432
EVAL_MCP_PORT ?= 53000
EVAL_COMPOSE := EVAL_DB_PORT=$(EVAL_DB_PORT) EVAL_MCP_PORT=$(EVAL_MCP_PORT) \
	$(COMPOSE) -f docker-compose.eval.yaml
EVAL_ENV := EVAL_DATABASE_URL=postgresql://eval@127.0.0.1:$(EVAL_DB_PORT)/eval \
	EVAL_MCP_URL=http://127.0.0.1:$(EVAL_MCP_PORT)

eval-up:  ## Start the throwaway eval stack and index eval/corpus/alpha and beta into it
	$(EVAL_COMPOSE) up -d --wait postgres mcp-server
	$(EVAL_COMPOSE) --profile index run --rm graphify
	$(EVAL_COMPOSE) --profile index run --rm graphify-beta

eval-down:  ## Remove the eval stack and its database
	$(EVAL_COMPOSE) --profile index down -v --remove-orphans

# The benchmark goes first: the tool check writes and restores, and a failed
# restore should not be what the benchmark scores. ARGS= reaches the benchmark.
eval: require-venv  ## Run the benchmark, SQL and MCP checks against the eval stack
	cd $(MCP_DIR) && $(EVAL_ENV) npm run eval -- $(ARGS)
	@$(MAKE) --no-print-directory eval-checks

eval-checks: require-venv  ## Run the SQL and MCP tool tests against a running eval stack
	cd $(MCP_DIR) && $(EVAL_ENV) npm test
	$(EVAL_ENV) $(VENV)/bin/pytest -q -m db

test: test-mcp test-py test-eval  ## Run every test suite, the stack ones on a throwaway eval stack

test-mcp:  ## Typecheck the MCP server, then run its tests that need no stack (ARGS= reaches vitest)
	@$(MAKE) --no-print-directory -C $(MCP_DIR) typecheck
	@$(MAKE) --no-print-directory -C $(MCP_DIR) test ARGS='$(ARGS)'

test-py: require-venv  ## Run the Python tests that need no stack (ARGS= reaches pytest)
	$(VENV)/bin/pytest -q $(ARGS)

# Built first so the stack runs the working tree, not the last `make build`.
# The stack is removed whether the checks pass or not, and their status wins.
test-eval: require-venv  ## Build, start the eval stack, run the benchmark, SQL and MCP tool tests, remove it
	@$(MAKE) --no-print-directory build-py
	@$(MAKE) --no-print-directory -C $(MCP_DIR) \
		IMAGE='$(MCP_IMAGE)' TAG='$(TAG)' build
	@$(MAKE) --no-print-directory eval-up
	@status=0; $(MAKE) --no-print-directory eval || status=$$?; \
		$(MAKE) --no-print-directory eval-down; exit $$status

# The same throwaway stack as test-eval; only the benchmark runs, and it rewrites
# eval/baseline.<mode>.json instead of gating against it.
eval-baseline: require-venv  ## Build, start the eval stack, re-record the benchmark baseline, remove it
	@$(MAKE) --no-print-directory build-py
	@$(MAKE) --no-print-directory -C $(MCP_DIR) \
		IMAGE='$(MCP_IMAGE)' TAG='$(TAG)' build
	@$(MAKE) --no-print-directory eval-up
	@status=0; (cd $(MCP_DIR) && $(EVAL_ENV) npm run eval -- --update-baseline $(ARGS)) \
		|| status=$$?; $(MAKE) --no-print-directory eval-down; exit $$status

# Each suggestion that kept its queries and names nodes is a question with a
# known answer, asked again of the running stack.
replay:  ## Replay the queries suggestions recorded against the live MCP server, reporting hit@k
	cd $(MCP_DIR) && REPLAY_MCP_URL=http://127.0.0.1:$(GATEWAY_PORT) npm run replay

require-venv:
	@test -x $(PYTHON) || { \
		echo "$(VENV) is missing or broken, run 'make init' first" >&2; \
		exit 1; \
	}

require-env:
	@test -f .env || { \
		echo ".env is missing, copy .env.example and fill it in" >&2; \
		exit 1; \
	}
	@grep -qE '^WORKER_API_TOKEN=.{16,}' .env || { \
		sed -i '/^WORKER_API_TOKEN=/d' .env; \
		printf 'WORKER_API_TOKEN=%s\n' "$$(openssl rand -hex 24)" >> .env; \
		echo "Generated WORKER_API_TOKEN in .env: the API serves file text" \
			"and refuses to start without one." >&2; \
	}

# Under sudo the agents' own state lives in /root, so an install would land
# where the user running them never looks.
require-not-root:
	@test -z "$$SUDO_USER" || { \
		echo "Run this without sudo: it registers the server for $$SUDO_USER" >&2; \
		exit 1; \
	}
