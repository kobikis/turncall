#!/usr/bin/env bash
# Run the WebRTC browser client (Vite dev server). Installs deps on first run.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
[[ -d node_modules ]] || npm install
exec npm run dev -- "$@"
