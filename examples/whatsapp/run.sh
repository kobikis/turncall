#!/usr/bin/env bash
# Run the whatsapp example. Required values come from the environment or the
# repo-root .env; anything else passes straight through to setup.py.
# NOTE: also pass --whatsapp-number +1555…  (required)
# Usage: ./run.sh --whatsapp-number +1555… [extra args]
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

WHATSAPP_PHONE_NUMBER_ID="$(envval WHATSAPP_PHONE_NUMBER_ID)"

missing=()
[[ -n "$WHATSAPP_PHONE_NUMBER_ID" ]] || missing+=(WHATSAPP_PHONE_NUMBER_ID)
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Missing: ${missing[*]} — set in the environment or in $ROOT/.env" >&2
  exit 1
fi

exec "$PY" "$DIR/setup.py" --whatsapp-phone-number-id "$WHATSAPP_PHONE_NUMBER_ID" "$@"
