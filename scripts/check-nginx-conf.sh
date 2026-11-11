#!/usr/bin/env bash
# There is no nginx linter in the pre-commit ecosystem, so the hook covering
# the .conf extension asks nginx itself. The config is meaningless on its own
# - it is a conf.d fragment - so it is mounted into a throwaway container
# holding the stock nginx.conf that includes it. A machine without docker
# skips the check rather than failing a commit it cannot run.
set -euo pipefail

image="nginx:1.30.6-alpine@sha256:583b7f2fce1cdb8da5ad5c39ebd3c33b98b87d493190e548bdee1fa8c619c311"
config="nginx/default.conf"

if ! command -v docker > /dev/null 2>&1; then
    echo "docker not found, skipping the nginx config check" >&2
    exit 0
fi

if ! docker info > /dev/null 2>&1; then
    echo "docker is not usable, skipping the nginx config check" >&2
    exit 0
fi

exec docker run --rm \
    -e AUTH_ENFORCE=off \
    -e GATEWAY_HOSTS=localhost:3000 \
    -v "$PWD/$config:/etc/nginx/conf.d/default.conf:ro" \
    -v "$PWD/nginx/templates:/etc/nginx/templates:ro" \
    "$image" nginx -t
