#!/usr/bin/env bash
# Reject a .ico file that does not start with the ICO header.
set -euo pipefail

status=0
for file in "$@"; do
  if [ "$(head -c 4 "$file" | od -An -tx1 | tr -d ' \n')" != "00000100" ]; then
    echo "$file: not an ICO image" >&2
    status=1
  fi
done
exit "$status"
