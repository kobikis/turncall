#!/usr/bin/env bash
# Seed a dev project + API key + published agent.
#
# TURNCALL_NUMBER is optional. Without it you get an agent you can talk to in
# the browser over WebRTC — no Twilio, no phone number, no tunnel. With it, the
# number is also bound to the agent so inbound calls resolve instead of
# 'twilio_inbound_unknown_number'.
#
# Requires the server running and reachable at BASE_URL.
#
# Usage:
#   bash scripts/seed_dev.sh                            # browser (WebRTC) only
#   TURNCALL_NUMBER=+15551234567 bash scripts/seed_dev.sh   # also bind a number
#
# With a number, use EXACTLY what Twilio sends as the `To` (see the to_number= log).
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8090}"
NUMBER="${TURNCALL_NUMBER:-}"   # optional — unset means browser/WebRTC only
PN_SID="${TWILIO_PN_SID:-PN00000000000000000000000000000000}"  # real PN SID optional for local testing

# Bootstrap (project/key creation) is platform-gated. Fall back to .env.
if [[ -z "${PLATFORM_API_KEY:-}" && -f .env ]]; then
  PLATFORM_API_KEY=$(grep -E '^PLATFORM_API_KEY=' .env | tail -1 | cut -d= -f2-)
fi
PK="${PLATFORM_API_KEY:?Set PLATFORM_API_KEY (see env.example) — bootstrap endpoints require it}"

# Extract a field from a {"success":true,"data":{...}} response body on stdin.
# python3, not python: macOS 12.3+ and Ubuntu 20.04+ ship no bare `python`,
# and the browser quickstart tells people they do not need Python at all.
json() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

echo "→ creating project"
PID=$(curl -fsS -X POST "$BASE/v1/projects" \
  -H "X-Platform-Key: $PK" \
  -H 'Content-Type: application/json' -d '{"name":"dev"}' | json "['data']['id']")

echo "→ creating first API key"
TC=$(curl -fsS -X POST "$BASE/v1/api-keys?project_id=$PID" \
  -H "X-Platform-Key: $PK" \
  -H 'Content-Type: application/json' -d '{"name":"dev","role":"admin"}' | json "['data']['raw_key']")

echo "→ creating agent (draft)"
AID=$(curl -fsS -X POST "$BASE/v1/agents" \
  -H "Authorization: Bearer $TC" -H 'Content-Type: application/json' -d '{
    "name": "receptionist",
    "config": {
      "system_prompt": "You are a friendly receptionist. Keep replies short and natural.",
      "first_message": "Thanks for calling! How can I help?",
      "stt": {"provider": "deepgram", "model": "nova-3-general"},
      "llm": {"provider": "openai", "model": "gpt-4o-mini"},
      "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"}
    }
  }' | json "['data']['id']")

echo "→ publishing agent"
curl -fsS -X POST "$BASE/v1/agents/$AID/publish" -H "Authorization: Bearer $TC" >/dev/null

if [[ -n "$NUMBER" ]]; then
  echo "→ binding number $NUMBER → agent"
  curl -fsS -X POST "$BASE/v1/phone-numbers" \
    -H "Authorization: Bearer $TC" -H 'Content-Type: application/json' -d "{
      \"external_number_sid\": \"$PN_SID\",
      \"e164_number\": \"$NUMBER\",
      \"routing_target_type\": \"agent\",
      \"routing_target_id\": \"$AID\"
    }" >/dev/null
fi

echo
if [[ -n "$NUMBER" ]]; then
  echo "✅ seeded — call $NUMBER now"
else
  echo "✅ seeded — talk to it in your browser:"
  echo "     cd examples/webrtc-client && npm install && npm run dev"
  echo "   then open http://localhost:5174 and paste the key and agent id below."
  echo "   (To answer real phone calls instead, re-run with TURNCALL_NUMBER set.)"
fi
echo "   API key (save it, shown once): $TC"
echo "   agent_id: $AID   project_id: $PID"
