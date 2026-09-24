#!/usr/bin/env bash
# Split one CPU and memory budget for the whole stack into per-service limits.
#
# Prints NAME=value lines the Makefile exports before any compose call. The
# budget is STACK_CPUS and STACK_MEM, read from the environment first and from
# the compose environment (.env) after; a per-service variable already set
# wins over its computed share. Memory is floored to a power of two in MiB.

set -euo pipefail

COMPOSE=${COMPOSE:-docker compose}

# service prefix, CPU percent, memory percent
SHARES=(
  "PG 30 34"
  "API 30 34"
  "EMBED 15 10"
  "VALKEY 5 10"
  "MCP 8 4"
  "VIEWER 5 3"
  "WEB 4 3"
  "NGINX 3 2"
)

declare -A FROM_COMPOSE=()

load_compose() {
  local key value
  while IFS='=' read -r key value; do
    if [[ ${key} =~ ^[A-Z]+_(CPUS|MEM|SHARED_BUFFERS|MAXMEMORY)$ ]]; then
      FROM_COMPOSE[${key}]=${value}
    fi
  done < <(${COMPOSE} config --environment 2> /dev/null || true)
}

setting() {
  local name=$1 fallback=$2 value
  value=${!name:-${FROM_COMPOSE[${name}]:-}}
  printf '%s' "${value:-${fallback}}"
}

to_mib() {
  local raw=${1,,}
  case ${raw} in
    *g | *gb) echo $((${raw%%g*} * 1024)) ;;
    *m | *mb) echo "${raw%%m*}" ;;
    *) echo $((raw / 1048576)) ;;
  esac
}

pow2_floor() {
  local value=$1 result=32
  while ((result * 2 <= value)); do
    result=$((result * 2))
  done
  echo "${result}"
}

main() {
  local cpus mem_mib line prefix cpu_pct mem_pct cpu_value mem_value
  load_compose
  cpus=$(setting STACK_CPUS 4)
  mem_mib=$(to_mib "$(setting STACK_MEM 6g)")

  for line in "${SHARES[@]}"; do
    read -r prefix cpu_pct mem_pct <<< "${line}"
    cpu_value=$(LC_ALL=C awk -v c="${cpus}" -v p="${cpu_pct}" 'BEGIN { printf "%.2f", c * p / 100 }')
    mem_value=$(pow2_floor $((mem_mib * mem_pct / 100)))

    echo "${prefix}_CPUS=$(setting "${prefix}_CPUS" "${cpu_value}")"
    echo "${prefix}_MEM=$(setting "${prefix}_MEM" "${mem_value}m")"
  done

  local pg_mem valkey_mem
  pg_mem=$(to_mib "$(setting PG_MEM "$(pow2_floor $((mem_mib * 34 / 100)))m")")
  valkey_mem=$(to_mib "$(setting VALKEY_MEM "$(pow2_floor $((mem_mib * 10 / 100)))m")")
  echo "PG_SHARED_BUFFERS=$(setting PG_SHARED_BUFFERS "$((pg_mem / 4))MB")"
  echo "VALKEY_MAXMEMORY=$(setting VALKEY_MAXMEMORY "$((valkey_mem * 3 / 4))mb")"
}

main "$@"
