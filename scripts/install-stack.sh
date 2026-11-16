#!/usr/bin/env bash
# Install, update or remove the enggraph stack from a release, with no checkout.

set -euo pipefail

repo="${ENGGRAPH_REPO:-oberon-systems/enggraph}"
compose_file=docker-compose.release.yaml
# What the release compose file mounts or reads beside itself.
release_files=(nginx/default.conf nginx/templates/auth.conf.template .env.example)
# The same list `make up` starts.
services=(postgres valkey embedder worker-api embed summarize mcp-server viewer web nginx)
# The embedder's weights, as worker/enggraph_worker/catalogue.py names them.
model_file=nomic-embed-text-v1.5.f16.gguf
model_url=https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/0188c9bf409793f810680a5a431e7b899c46104c/nomic-embed-text-v1.5.f16.gguf
model_sha256=f7af6f66802f4df86eda10fe9bbcfc75c39562bed48ef6ace719a251cf1c2fdb
# Fixed in docker-compose.yaml, so not choices of this script.
data_dir="$HOME/.local/share/enggraph/db"
model_dir="$HOME/.local/share/enggraph/models"
min_free_kib=$((3 * 1024 * 1024))

dir="$HOME/.local/share/enggraph/stack"
release=""
port=""
bind=""
hosts=()
operator=""
purge=""
yes=""

usage() {
    cat << EOF
usage: install-stack.sh [update|doctor|uninstall] [flags]

Without a command it installs the newest stack release and starts it. Over a
stack already installed it changes nothing and says which release runs.

  update     back the database up, then move to the newest release (or
             --release), keeping .env and the data
  doctor     check that this host can run the stack, changing nothing
  uninstall  stop the stack and remove its directory; --purge also deletes
             the database

  --dir DIR           where the stack's files live
                      (default: ~/.local/share/enggraph/stack)
  --release X.Y.Z     stack release to install (default: the newest)
  --port PORT         the gateway port (default: 3000)
  --bind ADDR         the interface the gateway binds (default: 127.0.0.1;
                      0.0.0.0 lets agents on other machines reach it)
  --host NAME         a name other machines reach the stack by; repeatable
  --operator NAME     who the first operator token is issued to
                      (default: $(id -un))
  --purge             with uninstall: delete the database as well
  --yes               never ask
  --help              show this
EOF
}

fail() {
    echo "install-stack: $*" >&2
    exit 1
}

say() {
    echo "install-stack: $*"
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
        --dir) need_value "$1" $# && dir="$2" && shift ;;
        --release) need_value "$1" $# && release="${2#v}" && shift ;;
        --port) need_value "$1" $# && port="$2" && shift ;;
        --bind) need_value "$1" $# && bind="$2" && shift ;;
        --host) need_value "$1" $# && hosts+=("$2") && shift ;;
        --operator) need_value "$1" $# && operator="$2" && shift ;;
        --purge) purge=1 ;;
        --yes) yes=1 ;;
        -h | --help) usage && exit 0 ;;
        *) fail "unknown argument $1, see --help" ;;
    esac
    shift
done

interactive() {
    [ -z "$yes" ] && (: < /dev/tty) 2> /dev/null
}

compose() {
    docker compose --project-directory "$dir" -f "$dir/$compose_file" "$@"
}

installed() {
    [ -f "$dir/.env" ] && [ -f "$dir/$compose_file" ]
}

env_value() {
    sed -n "s/^$1=//p" "$dir/.env" | tail -n 1
}

# set_env KEY VALUE FILE
set_env() {
    [ -f "$3" ] || fail "no $3 to set $1 in"
    if grep -q "^$1=" "$3"; then
        sed -i "s|^$1=.*|$1=$2|" "$3"
    else
        printf '%s=%s\n' "$1" "$2" >> "$3"
    fi
}

random_secret() {
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

# The working directory compose records on the containers of project enggraph.
running_from() {
    docker ps --filter label=com.docker.compose.project=enggraph \
        --format '{{.Label "com.docker.compose.project.working_dir"}}' 2> /dev/null | head -n 1
}

gateway_url() {
    local address="${bind:-127.0.0.1}"
    case "$address" in
        0.0.0.0 | 127.* | localhost) address=127.0.0.1 ;;
    esac
    printf 'http://%s:%s' "$address" "${port:-3000}"
}

problems=""

problem() {
    problems="$problems
  - $1
    fix: $2"
}

doctor() {
    local elsewhere existing free
    [ "$(uname -s)" = Linux ] ||
        problem "the stack is run on Linux, this is $(uname -s)" "use a Linux host"
    if ! command -v docker > /dev/null; then
        problem "no docker" "install Docker Engine: https://docs.docker.com/engine/install/"
    elif ! docker info > /dev/null 2>&1; then
        problem "$(id -un) cannot reach the Docker daemon" "start Docker, or add $(id -un) to the docker group and log in again"
    elif ! docker compose version > /dev/null 2>&1; then
        problem "no Docker Compose plugin" "install docker-compose-plugin"
    else
        elsewhere="$(running_from)"
        if [ -n "$elsewhere" ] && [ "$elsewhere" != "$dir" ]; then
            problem "a stack named enggraph already runs from $elsewhere" \
                "run 'docker compose down' there first, or go on using that one"
        fi
    fi
    if [ "$command" = install ] && (: > "/dev/tcp/127.0.0.1/${port:-3000}") 2> /dev/null; then
        problem "something already listens on port ${port:-3000}" "pass --port with a free one"
    fi
    existing="$data_dir"
    while [ ! -d "$existing" ]; do existing="$(dirname "$existing")"; done
    free="$(df -Pk "$existing" | awk 'NR == 2 { print $4 }')"
    [ "${free:-0}" -ge "$min_free_kib" ] ||
        problem "less than 3 GiB free under $existing" "free some space there"
    if [ -n "$problems" ]; then
        echo "install-stack: doctor found problems:$problems" >&2
        return 1
    fi
    say "doctor: this host can run the stack"
}

latest_release() {
    curl -fsSL "https://api.github.com/repos/$repo/releases/latest" |
        sed -n 's/.*"tag_name": *"v\([0-9][0-9.]*\)".*/\1/p' | head -n 1
}

fetch_release() {
    local file raw
    if [ -z "$release" ]; then
        release="$(latest_release)" || fail "cannot reach github.com/$repo"
        [ -n "$release" ] || fail "github.com/$repo has no stack release yet"
    fi
    curl -fsSL -o "$work/$compose_file" \
        "https://github.com/$repo/releases/download/v$release/$compose_file" ||
        fail "release v$release carries no $compose_file"
    if grep -E '^[[:space:]]*image:' "$work/$compose_file" | grep -vq '@sha256:'; then
        fail "an image of release v$release is not named by digest"
    fi
    raw="https://raw.githubusercontent.com/$repo/v$release"
    for file in "${release_files[@]}"; do
        mkdir -p "$work/$(dirname "$file")"
        curl -fsSL -o "$work/$file" "$raw/$file" || fail "cannot download $file of v$release"
    done
}

place_files() {
    local file
    install -d -m 0755 "$dir"
    for file in "$compose_file" "${release_files[@]}"; do
        install -D -m 0644 "$work/$file" "$dir/$file"
    done
}

write_env() {
    local name list=""
    (
        umask 077
        cp "$work/.env.example" "$dir/.env"
    )
    set_env POSTGRES_PASSWORD "$(random_secret)" "$dir/.env"
    set_env POSTGRES_APP_PASSWORD "$(random_secret)" "$dir/.env"
    set_env WORKER_API_TOKEN "$(random_secret)" "$dir/.env"
    set_env GATEWAY_PORT "${port:-3000}" "$dir/.env"
    set_env GATEWAY_BIND "${bind:-127.0.0.1}" "$dir/.env"
    set_env TAG "$release" "$dir/.env"
    if [ "${port:-3000}" != 3000 ] || [ "${#hosts[@]}" -ne 0 ]; then
        list="localhost:${port:-3000},127.0.0.1:${port:-3000},[::1]:${port:-3000}"
        for name in ${hosts[@]+"${hosts[@]}"}; do
            list="$list,$name:${port:-3000}"
        done
        set_env GATEWAY_HOSTS "$list" "$dir/.env"
    fi
    chmod 600 "$dir/.env"
}

fetch_model() {
    local target="$model_dir/$model_file"
    if [ -f "$target" ] && echo "$model_sha256  $target" | sha256sum -c - > /dev/null 2>&1; then
        return 0
    fi
    mkdir -p "$model_dir"
    say "downloading $model_file for the embedder"
    curl -fL --proto '=https' -o "$target.part" "$model_url" || fail "cannot download $model_file"
    echo "$model_sha256  $target.part" | sha256sum -c - > /dev/null 2>&1 || {
        rm -f "$target.part"
        fail "$model_file does not match its digest"
    }
    mv -f "$target.part" "$target"
}

wait_healthy() {
    local tries=0
    while [ "$tries" -lt 90 ]; do
        if curl -fsS --max-time 5 -o /dev/null "$(gateway_url)/health" 2> /dev/null; then
            return 0
        fi
        sleep 2
        tries=$((tries + 1))
    done
    fail "the stack does not answer at $(gateway_url)/health; see: docker compose --project-directory $dir logs"
}

start_stack() {
    mkdir -p "$data_dir"
    compose --profile index pull --quiet
    compose up -d "${services[@]}"
    wait_healthy
}

issue_operator() {
    compose exec -T web python -m enggraph.core.authn issue \
        --kind operator --name "${operator:-$(id -un)}"
}

install_stack() {
    local token
    if installed; then
        if [ -n "$release$port$bind$operator$purge" ] || [ "${#hosts[@]}" -ne 0 ]; then
            fail "the stack is installed in $dir; run update or uninstall"
        fi
        say "the stack $(env_value TAG) is installed in $dir; run update to update it or uninstall to remove it"
        return 0
    fi
    doctor
    fetch_release
    fetch_model
    place_files
    write_env
    start_stack
    token="$(issue_operator)" || fail "the stack runs, but no operator token was issued"
    say "the stack $release runs at $(gateway_url), its files are in $dir"
    say "operator token for ${operator:-$(id -un)}, shown once - log in to the dashboard with it:"
    echo "$token"
    say "next, on each machine whose code is indexed: install-agent.sh --stack-url $(gateway_url)"
}

backup_database() {
    local file
    install -d -m 0700 "$dir/backups"
    file="$dir/backups/enggraph-$(date -u +%Y%m%dT%H%M%SZ).dump"
    compose exec -T postgres pg_dump -Fc -U "$(env_value POSTGRES_USER)" -d "$(env_value POSTGRES_DB)" > "$file" || {
        rm -f "$file"
        fail "the backup failed, nothing was updated"
    }
    say "backed the database up to $file"
}

update_stack() {
    local before
    installed || fail "no stack is installed in $dir; run this without update to install one"
    if [ -n "$port$bind$operator$purge" ] || [ "${#hosts[@]}" -ne 0 ]; then
        fail "update keeps .env and takes only --release"
    fi
    port="$(env_value GATEWAY_PORT)"
    bind="$(env_value GATEWAY_BIND)"
    before="$(env_value TAG)"
    fetch_release
    fetch_model
    if [ -n "$(running_from)" ]; then
        backup_database
        compose stop worker-api mcp-server web viewer
    fi
    place_files
    set_env TAG "$release" "$dir/.env"
    start_stack
    say "updated the stack: ${before:-unknown} -> $release"
}

confirm_removal() {
    local answer what="$dir"
    [ -z "$yes" ] || return 0
    interactive || fail "no terminal to confirm: pass --yes"
    [ -z "$purge" ] || what="$dir and the database in $data_dir"
    printf 'This stops the stack and removes %s.\nremove it? [y/N]: ' "$what" > /dev/tty
    read -r answer < /dev/tty || answer=""
    case "$answer" in
        [yY]*) ;;
        *) fail "nothing removed" ;;
    esac
}

# The database files belong to the container's postgres user, so a container
# of the same image deletes them.
purge_database() {
    local image
    image="$(sed -n 's/^[[:space:]]*image: *\(pgvector\/[^[:space:]]*\).*/\1/p' "$dir/$compose_file" | head -n 1)"
    [ -d "$data_dir" ] || return 0
    docker run --rm -v "$data_dir:/data" --entrypoint sh "$image" -c 'rm -rf /data/* /data/.[!.]*'
    rmdir "$data_dir"
}

remove_stack() {
    if ! installed; then
        say "no stack is installed in $dir"
        return 0
    fi
    confirm_removal
    if [ -n "$purge" ]; then
        compose --profile index down --remove-orphans --volumes
        purge_database
    else
        compose --profile index down --remove-orphans
    fi
    rm -rf "$dir"
    if [ -n "$purge" ]; then
        say "removed the stack and its database; the model stays in $model_dir"
    else
        say "removed the stack; the database stays in $data_dir"
    fi
}

command -v curl > /dev/null || fail "curl is required"
command -v sha256sum > /dev/null || fail "sha256sum is required"

case "$command" in
    doctor)
        doctor
        exit 0
        ;;
    uninstall)
        remove_stack
        exit 0
        ;;
esac

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [ "$command" = update ]; then
    update_stack
else
    install_stack
fi
