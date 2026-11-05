#!/usr/bin/env bash
# Serve eval/corpus to the eval stack through a real host agent, the one way any
# stack reads a tree. Reached through `make eval-up` and `make eval-down`.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

read -r -a compose <<< "${COMPOSE:-docker compose}"
compose+=(-f docker-compose.eval.yaml)
work="$repo_root/.eval-agent"

stop_agent() {
    if [ -f "$work/pid" ]; then
        kill "$(cat "$work/pid")" 2> /dev/null || true
    fi
    rm -rf "$work"
}

start_agent() {
    local corpus project
    local agent=("${AGENT_BIN:?AGENT_BIN names the built agent}" --config "$work/config.toml")
    stop_agent
    mkdir -p "$work"
    corpus="$(cd eval/corpus && pwd -P)"
    "${agent[@]}" init --stack-url "http://127.0.0.1:${EVAL_GATEWAY_PORT:-53004}" \
        --name eval --state-dir "$work/state" > /dev/null
    for project in alpha beta; do
        "${agent[@]}" project add "$project" "$corpus/$project" > /dev/null
    done
    "${agent[@]}" run > "$work/log" 2>&1 &
    echo "$!" > "$work/pid"
}

# In the service holding the socket: what an operator does on the Agents page.
approve_and_bind() {
    "${compose[@]}" exec -T worker-api python - << 'PYTHON'
import json
import os
import time
import urllib.error
import urllib.request

from enggraph.core import agentauth

PROJECTS = ("alpha", "beta")
DEADLINE = time.monotonic() + 120


def until(what, found):
    while time.monotonic() < DEADLINE:
        answer = found()
        if answer:
            return answer
        time.sleep(1)
    raise SystemExit(f"timed out waiting for {what}")


def registered():
    rows = agentauth.list_registrations()
    return [row["agent_id"] for row in rows if row["name"] == "eval"]


def announced():
    return PROJECTS if all(
        any(row["agent_id"] == agent_id and row["name"] == project
            for row in agentauth.list_exports())
        for project in PROJECTS
    ) else None


def connected(project):
    request = urllib.request.Request(
        f"http://127.0.0.1:3003/internal/trees/{project}/available",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {os.environ['WORKER_API_TOKEN']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:
            return json.load(answer).get("available") is True
    except (OSError, ValueError):
        return False


agent_id = until("the agent to register", registered)[-1]
agentauth.approve_registration(agent_id, "eval")
until("the agent to announce its projects", announced)
for project in PROJECTS:
    agentauth.decide_export(agent_id, project, "approved", "eval")
    agentauth.bind_export(agent_id, project, "eval")
for project in PROJECTS:
    until(f"{project} to be served", lambda: connected(project))
PYTHON
}

case "${1:-}" in
    up)
        if [ -f "$work/pid" ] && kill -0 "$(cat "$work/pid")" 2> /dev/null; then
            exit 0
        fi
        start_agent
        if ! approve_and_bind; then
            sed 's/^/  /' "$work/log" >&2
            stop_agent
            exit 1
        fi
        echo "eval/corpus is served through the host agent"
        ;;
    down)
        stop_agent
        ;;
    *)
        echo "usage: $0 up|down" >&2
        exit 2
        ;;
esac
