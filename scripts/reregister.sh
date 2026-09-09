#!/usr/bin/env bash
# Rewrite the agent configuration of every onboarded codebase.
#
# Reached through `make reregister`. Onboarding writes `.mcp.json` and
# `.gemini/settings.json` once, so a codebase onboarded before the rename still
# addresses the server as `context`. This walks the projects table and runs the
# same registration step over each directory, which moves that entry under the
# new key and leaves everything else alone.
#
# A project whose AGENT_ROOT is not its source - a monorepo onboarded one slice
# at a time - is not reached: the files belong beside the tree the agent opens,
# not beside the slice. Run `enggraph-install` from that root instead.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

read -r -a compose <<< "${COMPOSE:-docker compose}"
python_bin="${PYTHON_BIN:-python3}"

env_value() {
    sed -n "s/^$1=//p" .env 2> /dev/null | tail -1
}

port="$(env_value GATEWAY_PORT)"
port="${port:-3000}"

errors="$(mktemp)"
trap 'rm -f "$errors"' EXIT
if ! listing="$("${compose[@]}" --profile index run --rm -T graphify \
        python -m enggraph.mounts 2> "$errors")"; then
    sed 's/^/  /' "$errors" >&2
    echo "Cannot list the projects. Is the stack up? Try 'make up'." >&2
    exit 1
fi

# Only the unnamed source is a whole tree an agent works in; a named one is a
# directory inside a project whose root the listing never names.
seen=0
while IFS=$'\t' read -r name alias root; do
    [ -n "$name" ] || continue
    [ "$alias" = "-" ] || continue
    if [ ! -d "$root" ]; then
        printf '%s\n' "$name: $root is not on this host, skipped"
        continue
    fi
    echo "$name"
    "$python_bin" scripts/mcp_register.py "$root" "$name" "$port"
    seen=$((seen + 1))
done <<< "$listing"

if [ "$seen" -eq 0 ]; then
    echo "No project reads a whole tree, so nothing was reregistered." >&2
fi
