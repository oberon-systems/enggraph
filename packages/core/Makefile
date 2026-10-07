# Schema revisions, applied with Alembic.
#
# Normally reached from the repo root as `make db <target>`, which passes
# COMPOSE down through $(MAKE) -C. Standalone use works too and falls back to
# the default below.

REPO_ROOT := $(abspath $(CURDIR)/../..)
VERSIONS := $(CURDIR)/src/enggraph/core/migrations/versions

# The migrate service is declared in the root compose file, and `make -C`
# leaves the working directory here, where there is none. So name the file.
COMPOSE ?= docker compose -f $(REPO_ROOT)/docker-compose.yaml
SERVICE := migrate
RUN := $(COMPOSE) run --rm $(SERVICE) python -m enggraph.core.migrate

NAME ?=
FORCE ?=

# `read -p` and the 10# arithmetic prefix in `new` are both bashisms.
SHELL := /bin/bash

.DEFAULT_GOAL := help

.PHONY: help migrate version validate rollback new

help:
	@echo "Revisions: $(VERSIONS)"
	@echo
	@echo "The revisions are baked into the api image: rebuild it after"
	@echo "writing one, then run migrate."
	@echo
	@echo "Targets:"
	@echo "  migrate   Apply every pending revision"
	@echo "  version   Show the revision the database is at, and the history"
	@echo "  validate  Check the revisions form one chain, without the database"
	@echo "  rollback  Undo the last applied revision (FORCE=1 skips the prompt)"
	@echo "  new       Write the next revision file, NAME=<slug>"

migrate:
	$(RUN) up

version:
	$(RUN) status

validate:
	$(RUN) validate

# A downgrade is a development tool, not an operating procedure: `make
# clean` at the repo root is the documented way to start the database over.
rollback:
	@test -n "$(FORCE)" || { \
		echo "This undoes the last applied revision."; \
		read -r -p "Continue? [y/N] " reply; \
		case "$$reply" in [yY]*) ;; *) echo "aborted"; exit 1 ;; esac; \
	}
	$(RUN) down

# Written here rather than by `alembic revision`: the image holds a copy of
# the revisions, and a file written inside a container would stay there.
new:
	@test -n "$(NAME)" || { echo "NAME= is required" >&2; exit 1; }
	@last=$$(ls $(VERSIONS)/[0-9]*.py 2> /dev/null | sed 's|.*/||' \
		| cut -d_ -f1 | sort -n | tail -1); \
	next=$$(printf '%04d' $$((10#$${last:-0} + 1))); \
	file="$(VERSIONS)/$${next}_$(NAME).py"; \
	test ! -e "$$file" || { echo "$$file exists" >&2; exit 1; }; \
	printf '%s\n' \
		'"""Say what this revision changes.' \
		'' \
		"Revision ID: $${next}" \
		"Revises: $${last}" \
		'"""' \
		'' \
		'from alembic import op' \
		'' \
		"revision = \"$${next}\"" \
		"down_revision = \"$${last}\"" \
		'branch_labels = None' \
		'depends_on = None' \
		'' \
		'' \
		'def upgrade() -> None:' \
		'    """Apply the change."""' \
		'    op.execute("")' \
		'' \
		'' \
		'def downgrade() -> None:' \
		'    """Undo the change."""' \
		'    op.execute("")' > "$$file"; \
	echo "$$file"
