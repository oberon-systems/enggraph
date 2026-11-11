#!/usr/bin/env bash
# Onboard one codebase onto the stack in a single pass: agent configuration
# and the permission that spares it a prompt per call, the instruction file
# and the file-selection pair. The skills come from the MCP server.
#
# The tree is read through its host agent, and compose is left alone.
#
# Reached through `make install`, which passes everything in the environment.
# Nothing here overwrites a file that exists - every step reports written,
# merged, kept or skipped, and the run ends with that list. Re-running it is
# how a piece added later gets picked up, never a way to reset one.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

target="${AGENT_ROOT:-$repo_root}"
# The tree the project reads. Usually AGENT_ROOT itself, and the literal
# "none" for a project registered ahead of any tree - an organization, which
# holds projects rather than files.
source_dir="${SOURCE:-$target}"
make_prefix="${MAKE_PREFIX:-make -C $repo_root}"
python_bin="${PYTHON_BIN:-python3}"
shell_rc="${SHELL_RC:-$HOME/.bashrc}"
# Whether a file already in the tree should be replaced by what this version
# would write. Without FORCE nothing is touched, which is what makes a re-run
# safe. With it, anything that differs is shown and asked about one file at a
# time: a yes here overwrites work someone did by hand.
offer() {
    local label="$1" existing="$2" candidate="$3" reply
    if [ ! -e "$existing" ]; then
        return 0
    fi
    if [ -z "${FORCE:-}" ]; then
        note "$label" "kept (already in the tree, FORCE=1 offers to replace)"
        return 1
    fi
    if cmp -s "$existing" "$candidate"; then
        note "$label" "kept (already current)"
        return 1
    fi
    # Opened rather than tested: /dev/tty exists as a path even where there is
    # no controlling terminal to open, and the test would pass while the read
    # printed an error and answered nothing.
    if ! { exec 3< /dev/tty; } 2> /dev/null; then
        note "$label" "kept (differs, but there is no terminal to ask at)"
        return 1
    fi
    echo
    echo "  $label differs from what this version writes:"
    diff -u "$existing" "$candidate" 2> /dev/null | tail -n +3 | sed 's/^/    /'
    printf '  replace %s? [y/N] ' "$label"
    read -r reply <&3 || reply=""
    exec 3<&-
    echo
    case "$reply" in
        [yY]*) return 0 ;;
        *) note "$label" "kept (declined)"; return 1 ;;
    esac
}

# The two fences earlier versions wrote shell aliases behind. There are no
# aliases any more - a project is registered from the dashboard or by
# `make install`, and neither needs a shell function - so both blocks are
# deleted wherever they are found rather than rewritten.
markers=(
    "# >>> enggraph >>>|# <<< enggraph <<<"
    "# >>> claude-context-mcp >>>|# <<< claude-context-mcp <<<"
)

if [ ! -d "$target" ]; then
    echo "AGENT_ROOT=$target is not a directory" >&2
    exit 1
fi
target="$(cd "$target" && pwd)"
if [ "$source_dir" != "none" ]; then
    if [ ! -d "$source_dir" ]; then
        echo "SOURCE=$source_dir is not a directory" >&2
        exit 1
    fi
    source_dir="$(cd "$source_dir" && pwd)"
fi

# The credentials and the port live in .env, which make does not source.
env_value() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -1
}

port="$(env_value GATEWAY_PORT)"
port="${port:-3000}"

# The token and the agent's claim cross the network in clear over plain http.
refuse_plain_http() {
    case "$1" in
        https://* | http://localhost | http://localhost[:/]* | \
            http://127.0.0.1 | http://127.0.0.1[:/]* | 'http://[::1]'*)
            return 0
            ;;
    esac
    if [ "${ALLOW_PLAIN_HTTP:-}" = "1" ]; then
        return 0
    fi
    echo "  $1 is plain http to another machine, so the token would cross" >&2
    echo "  the network in clear. Use https, or pass ALLOW_PLAIN_HTTP=1." >&2
    exit 1
}

if [ -n "${STACK_URL:-}" ]; then
    refuse_plain_http "$STACK_URL"
fi

summary=()
note() {
    summary+=("$(printf '  %-22s %s' "$1" "$2")")
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Onboarding $target"
echo

agent_binary() {
    local found
    for found in "$(command -v enggraph-agent || true)" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/enggraph/bin/enggraph-agent" \
        "${DESTDIR:-}/opt/enggraph/agent/bin/enggraph-agent"; do
        if [ -n "$found" ] && [ -x "$found" ]; then
            printf '%s\n' "$found"
            return 0
        fi
    done
    return 1
}

ensure_agent() {
    local wanted="${STACK_URL:-http://localhost:$port}" flags
    if agent_bin="$(agent_binary)" && "$agent_bin" status > /dev/null 2>&1; then
        return 0
    fi
    flags=(--stack-url "$wanted" --layout "${AGENT_LAYOUT:-xdg}")
    if [ -n "${AGENT_FROM_SOURCE:-}" ]; then
        flags+=(--from-source)
    else
        flags+=(--release "$(sed -n 's/^version = "\(.*\)"/\1/p' packages/agent/Cargo.toml | head -n 1)")
    fi
    bash scripts/install-agent.sh "${flags[@]}"
    agent_bin="$(agent_binary)" || {
        echo "  the installer left no enggraph-agent to run" >&2
        exit 1
    }
}

derived_name() {
    printf '%s' "${PROJECT_NAME:-$(basename "$target")}" | tr '[:upper:]' '[:lower:]' \
        | tr -c 'a-z0-9._-' '-' | sed 's/^[^a-z0-9]*//; s/-*$//'
}

wait_for_binding() {
    local offered="$1" deadline=$((SECONDS + ${AGENT_WAIT:-900})) line bound
    while :; do
        line="$("$agent_bin" project list | grep -F -- "$offered $source_dir " | head -n 1 || true)"
        bound="$(sed -n 's/.* bound to //p' <<< "$line")"
        if [ -n "$bound" ]; then
            project="$bound"
            return 0
        fi
        if [ "$line" = "$offered $source_dir refused" ]; then
            echo "  an operator refused $offered on $stack/agents; nothing was onboarded." >&2
            exit 1
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "  $offered is not bound yet. Is the agent running? 'enggraph-agent status' says." >&2
            echo "  Nothing is lost: bind it on $stack/agents and run this again." >&2
            exit 1
        fi
        sleep 5
    done
}

offer_through_agent() {
    local offered
    ensure_agent
    stack="$("$agent_bin" status | sed -n 's/^stack //p')"
    stack="${stack%/}"
    refuse_plain_http "$stack"
    if [ -n "${STACK_URL:-}" ] && [ "${STACK_URL%/}" != "$stack" ]; then
        echo "  this agent reports to $stack, not to $STACK_URL" >&2
        exit 1
    fi
    offered="$("$agent_bin" project list | grep -F -- " $source_dir " | head -n 1 | cut -d' ' -f1 || true)"
    if [ -z "$offered" ]; then
        offered="$(derived_name)"
        "$agent_bin" project add "$offered" "$source_dir" > /dev/null
    fi
    "$agent_bin" status | sed -n 's/^state /  the agent: /p'
    echo "  approve the agent, approve $offered and bind it on $stack/agents"
    wait_for_binding "$offered"
    note "agent project" "$offered, bound to $project"
}

stack_token() {
    if [ -n "${ENGGRAPH_TOKEN:-}" ]; then
        printf '%s' "$ENGGRAPH_TOKEN"
        return 0
    fi
    "$python_bin" -c 'import json, sys
print(json.load(open(sys.argv[1])).get("env", {}).get("ENGGRAPH_TOKEN", ""), end="")' \
        "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json" 2> /dev/null || true
}

# The token goes through stdin, since an argument would show in the process list.
stack_call() {
    local args=(-fsS --max-time 300 -X "$1" "$stack/worker/$2")
    [ $# -lt 3 ] || args+=(-H 'Content-Type: application/json' -d "$3")
    printf 'header = "Authorization: Bearer %s"\n' "$(stack_token)" | curl -K - "${args[@]}"
}

scan_through_agent() {
    if stack_call GET "projects/$project/scan" > "$work/scan" 2> "$work/scan.err"; then
        "$python_bin" -c 'import json, sys
print("\n".join(json.load(open(sys.argv[1]))["report"]))' "$work/scan" | sed 's/^/  /'
    else
        echo "  no report: the scan through the agent did not answer"
        sed 's/^/  /' "$work/scan.err"
    fi
}

store_project() {
    local body
    body="$("$python_bin" -c 'import json, sys
print(json.dumps({"name": sys.argv[1], "root_path": sys.argv[2], "project_type": sys.argv[3]}))' \
        "$project" "$1" "${TYPE:-}")"
    stack_call POST projects "$body" > /dev/null
}

# 1. Offer the tree and scan it through the agent, so the formats reported are
#    the ones the parser tables of the stack actually list.
project=""
echo "Agent"
if [ "$source_dir" = "none" ]; then
    stack="${STACK_URL:-http://localhost:$port}"
    stack="${stack%/}"
    project="$(derived_name)"
    echo "  the project reads no directory, so no agent is involved"
else
    source_dir="$(cd "$source_dir" && pwd -P)"
    offer_through_agent
fi
echo
echo "Formats"
if [ "$source_dir" = "none" ]; then
    echo "  the project reads no directory yet, so there is nothing to scan"
else
    scan_through_agent
fi

if [ "$source_dir" = "none" ]; then
    note "tree" "none: registered as a project that reads nothing yet"
else
    note "tree" "$source_dir"
fi

# 2. Both agents, one address - and, for Claude, a standing permission for
#    the read tools; a tool that writes goes on asking. That one lives in the
#    user's settings rather than the codebase's, so it is granted once and
#    holds for every project; PERMISSIONS=0 leaves the file alone and every
#    call goes on asking. GEMINI_TRUST=1 lets Gemini skip its own prompts.
echo
echo "Agents"
claude_settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
"$python_bin" scripts/mcp_register.py "$target" "$project" "$stack"
note "agent configuration" "$project at $stack"
if [ "${PERMISSIONS:-1}" = "0" ]; then
    note "claude permissions" "skipped (PERMISSIONS=0)"
else
    note "claude permissions" "$claude_settings"
fi

# 3. The instruction file. Claude reads CLAUDE.local.md beside its CLAUDE.md,
#    which is private and always written. Gemini has no .local convention, so
#    the same text goes to GEMINI.md - but only when a codebase has none, since
#    that name is where its own instructions to Gemini live.
echo
echo "Instructions"
render() {
    sed -e "s|@MAKE@|$make_prefix|g" -e "s|@ROOT@|$target|g" \
        -e "s|@PROJECT@|$project|g" -e "s|@FILE@|$1|g" templates/CLAUDE.local.md
}

for name in CLAUDE.local.md GEMINI.md; do
    if [ "$name" = "GEMINI.md" ]; then
        if ! command -v gemini > /dev/null; then
            note "$name" "skipped (gemini is not installed)"
            continue
        fi
        # GEMINI.md is Gemini's counterpart of CLAUDE.md, not of
        # CLAUDE.local.md: one already in the tree is the codebase's own.
        if [ -e "$target/$name" ]; then
            note "$name" "kept (the project's own instructions)"
            continue
        fi
    fi
    render "$name" > "$work/$name"
    if offer "$name" "$target/$name" "$work/$name"; then
        cp "$work/$name" "$target/$name"
        note "$name" "written"
    fi
done
echo "  rendered from templates/CLAUDE.local.md"

# 4. What earlier versions wrote into the rc file. A project used to be a
#    selection of host directories, and the aliases were how one was handed
#    the directory the shell stood in. Directories are gone, so the block is
#    taken out of the shell that still carries it rather than refreshed.
echo
echo "Shell"
# The walk that deletes one fence, markers included. Only ever called with
# both present: without a closing marker it would swallow everything after it.
drop_block() {
    awk -v start="$2" -v end="$3" '
        $0 == start { skipping = 1; next }
        skipping && $0 == end { skipping = 0; next }
        !skipping
    ' "$1"
}

dropped=0
for pair in "${markers[@]}"; do
    open="${pair%%|*}"
    close="${pair##*|}"
    if [ -f "$shell_rc" ] && grep -Fq "$open" "$shell_rc" \
            && grep -Fq "$close" "$shell_rc"; then
        # Rewritten through cat rather than mv, so the rc file keeps its own
        # inode and mode instead of inheriting the temporary file's.
        drop_block "$shell_rc" "$open" "$close" > "$work/rc"
        cat "$work/rc" > "$shell_rc"
        dropped=$((dropped + 1))
    elif [ -f "$shell_rc" ] && grep -Fq "$open" "$shell_rc"; then
        note "shell aliases" "left alone (the block has no closing marker)"
        echo "  $shell_rc opens $open and never closes it, so it cannot be"
        echo "  removed safely. Delete those lines by hand."
    fi
done
if [ "$dropped" -gt 0 ]; then
    note "shell aliases" "removed from $shell_rc"
    echo "  the enggraph block is gone from $shell_rc; open a new shell"
else
    note "shell aliases" "none to remove"
    echo "  $shell_rc defines none of ours"
fi

# 5. The row the rest of the stack addresses the tree by. Binding made the row
#    of a tree, so only what binding cannot say is sent.
echo
echo "Registration"
if [ "$source_dir" != "none" ] && [ -z "${TYPE:-}" ]; then
    note "project row" "$project, made by binding it"
    echo "  $project exists from the moment it is bound"
elif store_project "$([ "$source_dir" = "none" ] || printf '%s' "$source_dir")"; then
    note "project row" "$project registered"
    echo "  $project stored"
else
    note "project row" "failed: the token in ENGGRAPH_TOKEN needs the write-records scope"
    echo "  the stack did not store $project" >&2
fi

# 6. Whether the address just written answers anything yet.
echo
echo "Stack"
if curl -fsS "$stack/health" > /dev/null 2>&1; then
    note "mcp server" "reachable at $stack"
    echo "  reachable at $stack"
else
    note "mcp server" "unreachable at $stack"
    echo "  nothing answers at $stack yet."
    echo "  The address written into the agent files is not live until then."
fi

echo
echo "Summary"
printf '%s\n' "${summary[@]}"
