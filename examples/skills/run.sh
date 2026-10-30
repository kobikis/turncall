#!/usr/bin/env bash
# Run the skills example's setup. Needs PLATFORM_API_KEY (environment or the
# repo-root .env); arguments pass straight through to setup.py.
# Usage: ./run.sh [--provider openai|xai] [--webhook-url http://host.docker.internal:9100]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3

echo "REMINDER: start the tools' server first (separate terminal):" >&2
echo "  cd $DIR && uvicorn webhook_server:app --host 0.0.0.0 --port 9100" >&2

exec "$PY" "$DIR/setup.py" "$@"
