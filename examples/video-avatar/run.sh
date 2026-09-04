#!/usr/bin/env bash
# Run the video-avatar example. Required values come from the environment or the
# repo-root .env; anything else passes straight through to setup.py.
# Usage: ./run.sh [extra setup.py args]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3

exec "$PY" "$DIR/setup.py" "$@"
