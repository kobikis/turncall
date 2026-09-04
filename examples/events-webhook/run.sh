#!/usr/bin/env bash
# Run the events-webhook example. Required values come from the environment or the
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

TURNCALL_NUMBER="$(envval TURNCALL_NUMBER)"
TWILIO_PN_SID="$(envval TWILIO_PN_SID)"

missing=()
[[ -n "$TURNCALL_NUMBER" ]] || missing+=(TURNCALL_NUMBER)
[[ -n "$TWILIO_PN_SID" ]] || missing+=(TWILIO_PN_SID)
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Missing: ${missing[*]} — set in the environment or in $ROOT/.env" >&2
  exit 1
fi

echo "REMINDER: Start the receiver first (separate terminal): python webhook_server.py" >&2

exec "$PY" "$DIR/setup.py" --twilio-number "$TURNCALL_NUMBER" --twilio-number-sid "$TWILIO_PN_SID" "$@"
