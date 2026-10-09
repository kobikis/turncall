#!/usr/bin/env bash
# How many concurrent calls one pod carries (#200).
#
# Runs server.py — the real app on stub providers and a fake carrier — in the
# API image under a pod's CPU/memory budget, and client.py in a second
# container on the same network, so the load generator neither shares the
# pod's budget nor goes through Docker Desktop's port forwarding. Needs the
# local stack (`make docker-up-local`): its Postgres and network, and its image.
#
#   scripts/calls_per_pod/run.sh                         # default ramp, 1 CPU / 2 GiB
#   POD_CPUS=2 POD_MEMORY=4g scripts/calls_per_pod/run.sh --steps 10,20,40,60
#   POD_ARGS="-e OMP_NUM_THREADS=1" scripts/calls_per_pod/run.sh   # extra `docker run` args
#
# Every argument goes to client.py (see `client.py --help`). The full result
# lands in scripts/calls_per_pod/results/<timestamp>.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-turncall-local-turncall}"
NETWORK="${NETWORK:-turncall-local_turncall_nw}"
PG_CONTAINER="${PG_CONTAINER:-turncall-local-postgres-1}"
LOAD_DB="${LOAD_DB:-turncall_load}"
POD_CPUS="${POD_CPUS:-1}"
# POD_CPUSET=3 pins the pod to one core with no CFS quota instead (see the doc:
# a quota throttles the whole process whenever its threads burst past it).
POD_CPUSET="${POD_CPUSET:-}"
POD_MEMORY="${POD_MEMORY:-2g}"
CLIENT_CPUS="${CLIENT_CPUS:-3}"
POD=turncall-load-pod
# The stack's Redis, on an index of its own: every call takes and renews a
# live-call lease there (#224), which is per-call work a pod does in
# production. Without one the limit fails open and that work goes unmeasured.
# The load project sets no limit and the pod no LIMIT_* default, so a step is
# never refused for being over one.
LOAD_REDIS_URL="${LOAD_REDIS_URL:-redis://redis:6379/11}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$ROOT/scripts/calls_per_pod/results"

# A database of its own: the calls write real rows, and a shared one holds
# credentials this pod has no key for.
DATABASE_URL="postgresql+asyncpg://turncall:turncall@postgres:5432/$LOAD_DB"
if ! docker exec "$PG_CONTAINER" psql -U turncall -lqt | cut -d'|' -f1 | grep -qw "$LOAD_DB"; then
  docker exec "$PG_CONTAINER" createdb -U turncall "$LOAD_DB"
  docker exec "$PG_CONTAINER" psql -qU turncall -d "$LOAD_DB" -c 'CREATE EXTENSION IF NOT EXISTS vector'
fi
docker run --rm --network "$NETWORK" -e DATABASE_URL="$DATABASE_URL" \
  -v "$ROOT/alembic:/app/alembic:ro" -v "$ROOT/src:/app/src:ro" -e PYTHONPATH=/app/src \
  "$IMAGE" alembic upgrade head >/dev/null

mounts=(-v "$ROOT/src:/app/src:ro" -v "$ROOT/tests:/app/tests:ro" -v "$ROOT/scripts:/app/scripts")
docker rm -f "$POD" >/dev/null 2>&1 || true
trap 'docker logs "$POD" > "$ROOT/scripts/calls_per_pod/results/$STAMP-pod.log" 2>&1 || true; docker rm -f "$POD" >/dev/null 2>&1 || true' EXIT
if [ -n "$POD_CPUSET" ]; then cpu_limit="--cpuset-cpus $POD_CPUSET"; else cpu_limit="--cpus $POD_CPUS"; fi
docker run -d --name "$POD" --network "$NETWORK" \
  $cpu_limit --memory "$POD_MEMORY" --memory-swap "$POD_MEMORY" \
  "${mounts[@]}" -e PYTHONPATH=/app/src -e DATABASE_URL="$DATABASE_URL" \
  -e REDIS_URL="$LOAD_REDIS_URL" -e LOAD_PUBLIC_BASE_URL="http://$POD:8090" -e LOG_LEVEL="${LOG_LEVEL:-info}" \
  -e LOAD_GC_FREEZE="${LOAD_GC_FREEZE:-0}" \
  ${POD_ARGS:-} "$IMAGE" python scripts/calls_per_pod/server.py >/dev/null

echo "pod: $cpu_limit --memory $POD_MEMORY ($IMAGE); client: --cpus $CLIENT_CPUS"
echo "host: $(uptime)"
docker run --rm --network "$NETWORK" --cpus "$CLIENT_CPUS" "${mounts[@]}" \
  -e PYTHONPATH=/app/src "$IMAGE" python scripts/calls_per_pod/client.py \
  --pod "http://$POD:8090" --out "scripts/calls_per_pod/results/$STAMP.json" "$@"
echo "host: $(uptime)"
