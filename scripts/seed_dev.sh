#!/usr/bin/env bash
# Seed a dev project + API key + published agent, and bind your Twilio number to
# it, so inbound calls resolve instead of 'twilio_inbound_unknown_number'.
#
# Requires the server running (make run) and reachable at BASE_URL.
#
# Usage:
#   TURNCALL_NUMBER=+15551234567 bash scripts/seed_dev.sh
#   TURNCALL_NUMBER=+15551234567 TWILIO_PN_SID=PNxxxx BASE_URL=http://localhost:8090 bash scripts/seed_dev.sh
#
# Use EXACTLY the number Twilio sends as the `To` (see the to_number= log line).
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8090}"
NUMBER="${TURNCALL_NUMBER:?Set TURNCALL_NUMBER to your Twilio number in E.164, e.g. +15551234567}"
PN_SID="${TWILIO_PN_SID:-PN00000000000000000000000000000000}"  # real PN SID optional for local testing

# Bootstrap (project/key creation) is platform-gated. Fall back to .env.
if [[ -z "${PLATFORM_API_KEY:-}" && -f .env ]]; then
  PLATFORM_API_KEY=$(grep -E '^PLATFORM_API_KEY=' .env | tail -1 | cut -d= -f2-)
fi
PK="${PLATFORM_API_KEY:?Set PLATFORM_API_KEY (see env.example) — bootstrap endpoints require it}"

# Extract a field from a {"success":true,"data":{...}} response body on stdin.
json() { python -c "import sys,json;print(json.load(sys.stdin)$1)"; }

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
      "stt": {"provider": "deepgram", "model": "nova-2"},
      "llm": {"provider": "openai", "model": "gpt-4o-mini"},
      "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"}
    }
  }' | json "['data']['id']")

echo "→ publishing agent"
curl -fsS -X POST "$BASE/v1/agents/$AID/publish" -H "Authorization: Bearer $TC" >/dev/null

echo "→ binding number $NUMBER → agent"
curl -fsS -X POST "$BASE/v1/phone-numbers" \
  -H "Authorization: Bearer $TC" -H 'Content-Type: application/json' -d "{
    \"external_number_sid\": \"$PN_SID\",
    \"e164_number\": \"$NUMBER\",
    \"routing_target_type\": \"agent\",
    \"routing_target_id\": \"$AID\"
  }" >/dev/null

echo
echo "✅ seeded — call $NUMBER now"
echo "   API key (save it, shown once): $TC"
echo "   agent_id: $AID   project_id: $PID"
