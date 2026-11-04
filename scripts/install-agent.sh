#!/usr/bin/env bash
# Install, upgrade or remove the enggraph host agent and its systemd unit.
# The agent writes its own config: this only calls `init` and `project add`.

set -euo pipefail

repo="${ENGGRAPH_REPO:-oberon-systems/enggraph}"
binary=enggraph-agent
unit_name=enggraph-agent.service
# Marks linger this script turned on: linger found on belongs to someone else.
linger_mark="linger-enabled-by-installer"
# Only a dry run sets this; the binary finds the real paths with no flag.
destdir="${DESTDIR:-}"

layout=""
stack_url=""
name=""
labels=""
projects=()
systemd=""
linger=""
release=""
from_source=""
yes=""

usage() {
    cat << EOF
usage: install-agent.sh [update|doctor|uninstall] [flags]

Without a command it installs the agent. A value no flag gave is asked on the
terminal; --yes, or no terminal, makes a required one an error.

  update     replace the binary and restart a unit in use, asking nothing and
             keeping the layout, the config and the linger; it takes only
             --release and --from-source
  doctor     check that this host can run the agent, changing nothing
  uninstall  remove the unit, the binary, the config and the state

  --stack-url URL            the gateway of the stack, with no path
  --name NAME                what the operator sees (default: the host name)
  --labels A,B               words the operator sees beside the name
  --project NAME=PATH        a directory to offer; repeatable
  --layout xdg|system        xdg: your home, no sudo; system: /opt/enggraph/agent
  --systemd | --no-systemd   start the unit, or only write it (default: start)
  --linger | --no-linger     keep the user unit running without a login (xdg)
  --release X.Y.Z            agent release to install (default: the newest)
  --from-source              build it with 'make agent' from this checkout
  --yes                      never ask
  --help                     show this
EOF
}

fail() {
    echo "install-agent: $*" >&2
    exit 1
}

say() {
    echo "install-agent: $*"
}

need_value() {
    [ "$2" -ge 2 ] || fail "$1 needs a value"
}

command=install
case "${1:-}" in
    update | doctor | uninstall)
        command="$1"
        shift
        ;;
esac

while [ $# -gt 0 ]; do
    case "$1" in
        --stack-url) need_value "$1" $# && stack_url="$2" && shift ;;
        --name) need_value "$1" $# && name="$2" && shift ;;
        --labels) need_value "$1" $# && labels="$2" && shift ;;
        --project) need_value "$1" $# && projects+=("$2") && shift ;;
        --layout) need_value "$1" $# && layout="$2" && shift ;;
        --release) need_value "$1" $# && release="$2" && shift ;;
        --systemd) systemd=yes ;;
        --no-systemd) systemd=no ;;
        --linger) linger=yes ;;
        --no-linger) linger=no ;;
        --from-source) from_source=1 ;;
        --yes) yes=1 ;;
        -h | --help) usage && exit 0 ;;
        *) fail "unknown argument $1, see --help" ;;
    esac
    shift
done

interactive() {
    [ -z "$yes" ] && (: < /dev/tty) 2> /dev/null
}

# ask VAR QUESTION DEFAULT FLAG: keeps a value already set, else asks, else takes the default.
ask() {
    local answer
    [ -z "${!1}" ] || return 0
    if interactive; then
        printf '%s [%s]: ' "$2" "$3" > /dev/tty
        read -r answer < /dev/tty || answer=""
        printf -v "$1" '%s' "${answer:-$3}"
    elif [ -n "$3" ]; then
        printf -v "$1" '%s' "$3"
    else
        fail "no terminal to ask for the $2: pass $4"
    fi
}

ask_yes() {
    local answer
    [ -z "${!1}" ] || return 0
    if interactive; then
        printf '%s [Y/n]: ' "$2" > /dev/tty
        read -r answer < /dev/tty || answer=""
        case "$answer" in
            [nN]*) printf -v "$1" no ;;
            *) printf -v "$1" yes ;;
        esac
    else
        printf -v "$1" yes
    fi
}

paths() {
    case "$layout" in
        xdg)
            bin_dir="${XDG_DATA_HOME:-$HOME/.local/share}/enggraph/bin"
            config_file="${XDG_CONFIG_HOME:-$HOME/.config}/enggraph/config.toml"
            state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/enggraph"
            link="$HOME/.local/bin/$binary"
            unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
            ;;
        system)
            bin_dir="$destdir/opt/enggraph/agent/bin"
            config_file="$destdir/etc/enggraph/config.toml"
            state_dir="$destdir/var/lib/enggraph"
            link="$destdir/usr/local/bin/$binary"
            unit_dir="$destdir/etc/systemd/system"
            ;;
        *) fail "--layout is xdg or system, not $layout" ;;
    esac
}

detect_layout() {
    [ -z "$layout" ] || return 0
    if [ -f "$destdir/etc/enggraph/config.toml" ]; then layout=system; fi
    if [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/enggraph/config.toml" ]; then layout=xdg; fi
}

choose_layout() {
    detect_layout
    [ -z "$layout" ] || return 0
    interactive || fail "no terminal to choose a layout: pass --layout xdg or --layout system"
    cat > /dev/tty << EOF
Where should the agent live?
  xdg     under your home, a systemd user unit; no sudo
  system  /opt/enggraph/agent, /etc/enggraph, /var/lib/enggraph, a system unit
          that runs as $(id -un); needs sudo
EOF
    ask layout "layout" xdg --layout
}

as_root() {
    if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

# Writes with root only where the system layout puts a file.
put() {
    if [ "$layout" = system ]; then as_root "$@"; else "$@"; fi
}

systemctl_unit() {
    if [ "$layout" = system ]; then as_root systemctl "$@"; else systemctl --user "$@"; fi
}

unit_tool() {
    if [ "$layout" = system ]; then printf '%s' "$1"; else printf '%s --user' "$1"; fi
}

unit_hint() {
    printf '%s status %s and %s -u %s' "$(unit_tool systemctl)" "$unit_name" "$(unit_tool journalctl)" "$unit_name"
}

agent() {
    "$bin_dir/$binary" --config "$config_file" "$@"
}

configured_stack() {
    [ -f "$config_file" ] || return 0
    sed -n "s/^stack_url = [\"']\(.*\)[\"']\$/\1/p" "$config_file"
}

linger_on() {
    loginctl show-user "$(id -un)" -p Linger 2> /dev/null | grep -qx Linger=yes
}

problems=""

problem() {
    problems="$problems
  - $1
    fix: $2"
}

check_systemd() {
    if ! command -v systemctl > /dev/null; then
        problem "no systemctl" "pass --no-systemd and start the agent yourself"
    elif [ "$layout" = system ]; then
        [ -d /run/systemd/system ] ||
            problem "systemd is not running on this host" "pass --no-systemd and start the agent yourself"
    elif ! systemctl --user show-environment > /dev/null 2>&1; then
        problem "no systemd user manager to talk to" "log in through a session, or use --layout system"
    elif ! systemd-run --user --quiet --wait --collect \
        -p ProtectSystem=strict -p ProtectHome=read-only true > /dev/null 2>&1; then
        problem "the user manager cannot sandbox a unit the way this one asks" \
            "use --layout system, or allow this user unprivileged user namespaces"
    fi
}

doctor() {
    local entry tree
    case "$(uname -s)/$(uname -m)" in
        Linux/x86_64 | Linux/aarch64) ;;
        *) problem "the agent is built for Linux on x86_64 and aarch64, this is $(uname -s)/$(uname -m)" \
            "use one of those hosts" ;;
    esac
    [ "$(id -u)" != 0 ] ||
        problem "running as root; the unit runs as the user who installs it" \
            "run this as that user, sudo is used where it is needed"
    if [ "$layout" = system ] && [ "$(id -u)" != 0 ]; then
        command -v sudo > /dev/null || problem "the system layout needs sudo" "install sudo or use --layout xdg"
    fi
    [ "$systemd" != yes ] || check_systemd
    if [ -n "$stack_url" ]; then
        curl -fsS --max-time 10 -o /dev/null "${stack_url%/}/health" 2> /dev/null ||
            problem "the stack does not answer at ${stack_url%/}/health" "check --stack-url and that the stack is up"
    fi
    for entry in ${projects[@]+"${projects[@]}"}; do
        case "$entry" in
            *=/*) ;;
            *)
                problem "--project $entry is not NAME=PATH with an absolute path" "spell it like alpha=/srv/alpha"
                continue
                ;;
        esac
        tree="${entry#*=}"
        { [ -d "$tree" ] && [ -r "$tree" ] && [ -x "$tree" ]; } ||
            problem "$tree is not a directory $(id -un) can read" "fix the path or its permissions"
    done
    if [ -n "$problems" ]; then
        echo "install-agent: doctor found problems:$problems" >&2
        return 1
    fi
    say "doctor: this host can run the agent"
}

# A release of the agent is published with --latest=false, so releases/latest
# names the stack's and the newest agent has to be picked from the list.
latest_release() {
    curl -fsSL "https://api.github.com/repos/$repo/releases?per_page=100" |
        sed -n 's/.*"tag_name": *"agent-v\([0-9][0-9.]*\)".*/\1/p' | sort -V | tail -n 1
}

fetch_release() {
    local asset base
    if [ -z "$release" ]; then
        release="$(latest_release)" || fail "cannot reach github.com/$repo"
        [ -n "$release" ] || fail "github.com/$repo has no agent release yet; --from-source builds one here"
    fi
    asset="$binary-$release-$(uname -m)-linux-musl"
    base="https://github.com/$repo/releases/download/agent-v$release"
    curl -fsSL -o "$work/$asset" "$base/$asset" ||
        fail "cannot download $asset of agent-v$release; --from-source builds one here"
    curl -fsSL -o "$work/SHA256SUMS" "$base/SHA256SUMS" || fail "cannot download SHA256SUMS of agent-v$release"
    (cd "$work" && grep " $asset\$" SHA256SUMS | sha256sum -c - > /dev/null 2>&1) ||
        fail "$asset does not match its SHA256SUMS"
    built="$work/$asset"
}

build_source() {
    local root
    root="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." && pwd)"
    [ -f "$root/packages/agent/Cargo.toml" ] || fail "--from-source runs from a checkout of $repo"
    make --no-print-directory -C "$root" agent
    built="$root/packages/agent/target/$(uname -m)-unknown-linux-musl/release/$binary"
    [ -x "$built" ] || fail "'make agent' left no $built"
}

place_binary() {
    put install -d -m 0755 "$bin_dir" "$(dirname "$link")"
    put install -m 0755 "$built" "$bin_dir/$binary"
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        say "$link is not ours, so it stays; the agent is $bin_dir/$binary"
    else
        put ln -sfn "$bin_dir/$binary" "$link"
    fi
}

prepare_directories() {
    local owner group
    owner="$(id -un)"
    group="$(id -gn)"
    if [ "$layout" = system ]; then
        as_root install -d -m 0755 -o "$owner" -g "$group" "$(dirname "$config_file")"
        as_root install -d -m 0700 -o "$owner" -g "$group" "$state_dir"
    else
        mkdir -p "$(dirname "$config_file")"
        install -d -m 0700 "$state_dir"
    fi
}

write_config() {
    local flags
    if [ -f "$config_file" ]; then
        say "keeping $config_file"
        return 0
    fi
    flags=(init --stack-url "$stack_url" --name "$name" --state-dir "$state_dir")
    [ -z "$labels" ] || flags+=(--labels "$labels")
    agent "${flags[@]}" > /dev/null
    say "wrote $config_file"
}

offer_projects() {
    local entry project tree listed
    listed="$(agent project list)"
    for entry in ${projects[@]+"${projects[@]}"}; do
        project="${entry%%=*}"
        tree="$(realpath "${entry#*=}")"
        if grep -Fq -- "$project $tree " <<< "$listed"; then
            say "already offers $project"
            continue
        fi
        agent project add "$project" "$tree" > /dev/null
        say "offers $project, $tree"
    done
}

write_unit() {
    local user_line="" target=default.target
    if [ "$layout" = system ]; then
        user_line="User=$(id -un)"
        target=multi-user.target
    fi
    cat > "$work/$unit_name" << EOF
[Unit]
Description=enggraph host agent
After=network-online.target
Wants=network-online.target

[Service]
$user_line
ExecStart="$bin_dir/$binary" --config "$config_file" run
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths="$state_dir"
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=$target
EOF
    put install -d -m 0755 "$unit_dir"
    put install -m 0644 "$work/$unit_name" "$unit_dir/$unit_name"
}

start_unit() {
    [ "$systemd" = yes ] || return 0
    systemctl_unit daemon-reload
    systemctl_unit enable "$unit_name" > /dev/null 2>&1
    systemctl_unit restart "$unit_name"
    say "the agent runs as $unit_name, see: $(unit_hint)"
    if [ "$layout" = xdg ] && [ "$linger" = yes ] && ! linger_on; then
        if loginctl enable-linger "$(id -un)" 2> /dev/null || as_root loginctl enable-linger "$(id -un)"; then
            : > "$state_dir/$linger_mark"
        else
            say "linger stays off, so the agent stops when you log out"
        fi
    fi
}

report() {
    local page="${stack_url%/}/agents" standing="" tries=0
    if [ "$systemd" != yes ]; then
        say "start it with: $bin_dir/$binary --config $config_file run"
        say "it is approved at $page"
        return 0
    fi
    while [ "$tries" -lt 15 ]; do
        standing="$(agent status | sed -n 's/^state //p')"
        [ "$standing" = "not registered" ] || break
        sleep 1
        tries=$((tries + 1))
    done
    case "$standing" in
        *fingerprint*) say "compare fingerprint ${standing##* } on $page and approve the agent there" ;;
        "not registered") say "the agent has not registered yet; see: $(unit_hint)" ;;
        *) say "$standing; the projects it offers are decided at $page" ;;
    esac
}

confirm_removal() {
    local answer
    [ -z "$yes" ] || return 0
    interactive || fail "no terminal to confirm: pass --yes"
    printf 'This removes %s, %s, %s and %s, the credential included.\nremove all of it? [y/N]: ' \
        "$unit_name" "$bin_dir" "$config_file" "$state_dir" > /dev/tty
    read -r answer < /dev/tty || answer=""
    case "$answer" in
        [yY]*) ;;
        *) fail "nothing removed" ;;
    esac
}

remove_agent() {
    local turned_on=""
    detect_layout
    if [ -z "$layout" ]; then
        say "no agent is installed"
        return 0
    fi
    paths
    confirm_removal
    [ ! -f "$state_dir/$linger_mark" ] || turned_on=1
    if [ -f "$unit_dir/$unit_name" ]; then
        if [ "$systemd" != no ]; then
            systemctl_unit disable --now "$unit_name" > /dev/null 2>&1 || true
        fi
        put rm -f "$unit_dir/$unit_name"
        [ "$systemd" = no ] || systemctl_unit daemon-reload
    fi
    [ ! -L "$link" ] || put rm -f "$link"
    put rm -rf "$bin_dir" "$state_dir"
    put rm -f "$config_file"
    put rmdir "$(dirname "$config_file")" 2> /dev/null || true
    if [ "$layout" = system ]; then
        as_root rm -rf "$destdir/opt/enggraph"
    else
        rmdir "$(dirname "$bin_dir")" 2> /dev/null || true
    fi
    say "removed the agent; Delete on the Agents page forgets it on the stack"
    if [ -n "$turned_on" ] && linger_on; then
        say "this installer turned linger on; if nothing else needs it: loginctl disable-linger"
    fi
}

run_doctor() {
    detect_layout
    if [ -n "$layout" ]; then
        paths
        stack_url="${stack_url:-$(configured_stack)}"
    fi
    systemd="${systemd:-yes}"
    doctor
}

unit_in_use() {
    [ -f "$unit_dir/$unit_name" ] || return 1
    command -v systemctl > /dev/null || return 1
    systemctl_unit is-enabled --quiet "$unit_name" || systemctl_unit is-active --quiet "$unit_name"
}

agent_version() {
    agent --version 2> /dev/null | sed 's/.* //' || true
}

update_agent() {
    local before
    if [ -n "$stack_url$name$labels$layout$systemd$linger" ] || [ "${#projects[@]}" -ne 0 ]; then
        fail "update keeps what is installed and takes only --release and --from-source"
    fi
    detect_layout
    [ -n "$layout" ] || fail "no agent is installed; run this without update to install one"
    paths
    { [ -x "$bin_dir/$binary" ] && [ -f "$config_file" ]; } ||
        fail "no agent is installed; run this without update to install one"
    stack_url="$(configured_stack)"
    if unit_in_use; then systemd=yes; else systemd=no; fi
    doctor
    before="$(agent_version)"
    if [ -n "$from_source" ]; then build_source; else fetch_release; fi
    place_binary
    write_unit
    if [ "$systemd" = yes ]; then
        systemctl_unit daemon-reload
        systemctl_unit restart "$unit_name"
    fi
    say "updated the agent: ${before:-unknown} -> $(agent_version)"
    report
}

if [ "$command" = uninstall ]; then
    remove_agent
    exit 0
fi

command -v curl > /dev/null || fail "curl is required"
command -v sha256sum > /dev/null || fail "sha256sum is required"

if [ "$command" = doctor ]; then
    run_doctor
    exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [ "$command" = update ]; then
    update_agent
    exit 0
fi

choose_layout
paths

configured="$(configured_stack)"
if [ -n "$configured" ]; then
    if [ -n "$stack_url" ] && [ "${stack_url%/}" != "${configured%/}" ]; then
        fail "$config_file names $configured, not $stack_url; uninstall first to move to another stack"
    fi
    stack_url="$configured"
fi
ask stack_url "stack URL" "" --stack-url
if [ ! -f "$config_file" ]; then
    ask name "agent name" "$(uname -n)" --name
fi
if [ ! -f "$config_file" ] && interactive; then
    if [ -z "$labels" ]; then
        printf 'labels, comma separated [none]: ' > /dev/tty
        read -r labels < /dev/tty || labels=""
    fi
    if [ "${#projects[@]}" -eq 0 ]; then
        printf 'a project to offer, NAME=PATH [none]: ' > /dev/tty
        read -r answer < /dev/tty || answer=""
        [ -z "$answer" ] || projects+=("$answer")
    fi
fi
if command -v systemctl > /dev/null; then
    ask_yes systemd "install and start the systemd unit?"
else
    systemd="${systemd:-no}"
fi
if [ "$layout" = xdg ] && [ "$systemd" = yes ] && ! linger_on; then
    ask_yes linger "keep the agent running after you log out (loginctl enable-linger)?"
fi

doctor

if [ -n "$from_source" ]; then build_source; else fetch_release; fi
place_binary
prepare_directories
write_config
offer_projects
write_unit
start_unit
report
