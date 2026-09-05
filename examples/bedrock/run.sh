#!/usr/bin/env bash
# Run the bedrock example. Required values come from the environment or the
# repo-root .env; anything else passes straight through to setup.py.
# Usage: ./run.sh [extra setup.py args]     e.g. ./run.sh --mode s2s
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3

envval() {  # $1=VAR — environment wins, else read from repo-root .env
  local v="${!1:-}"
  if [[ -z "$v" && -f "$ROOT/.env" ]]; then
    v="$(grep -E "^$1=" "$ROOT/.env" | tail -1 | cut -d= -f2- || true)"
  fi
  printf '%s' "$v"
}

SERVER_URL="$(envval PUBLIC_BASE_URL)"
[[ -n "$SERVER_URL" ]] || SERVER_URL="http://localhost:8090"

exec "$PY" "$DIR/setup.py" --server-url "$SERVER_URL" "$@"
