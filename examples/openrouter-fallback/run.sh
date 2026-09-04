#!/usr/bin/env bash
# Run the openrouter-fallback example. Required values come from the environment or the
# repo-root .env; anything else passes straight through to setup.py.
# Usage: ./run.sh [extra setup.py args]
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

PUBLIC_BASE_URL="$(envval PUBLIC_BASE_URL)"

missing=()
[[ -n "$PUBLIC_BASE_URL" ]] || missing+=(PUBLIC_BASE_URL)
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Missing: ${missing[*]} — set in the environment or in $ROOT/.env" >&2
  exit 1
fi

exec "$PY" "$DIR/setup.py" --server-url "$PUBLIC_BASE_URL" "$@"
