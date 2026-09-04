# A/B Testing Example

Test two versions of a voice agent on the same phone number with weighted
traffic routing. Compare a concise style (v1) vs friendly style (v2).

## What it does

- **Creates two agent versions** with different personalities for the same "support-agent"
- **Auto-promotes** phone number when a new version is published
- **Rolls back** to a previous version with one API call
- **Splits traffic 50/50** between v1 and v2 for A/B testing
- **Deterministic routing** — same caller always reaches the same variant

## Prerequisites

1. **Twilio account** with a phone number
2. **OpenAI API key**
3. **Cartesia API key** (play.cartesia.ai)
4. **Docker** (for Postgres + Redis)
5. **ngrok** (to expose local server to Twilio)

## Quick Start

### 1. Configure environment

```bash
cp env.example .env
# Edit .env:
#   TWILIO_ACCOUNT_SID=ACxxxxxxxx
#   TWILIO_AUTH_TOKEN=xxxxxxxx
#   OPENAI_API_KEY=sk-xxxxxxxx
#   CARTESIA_API_KEY=sk_car_xxxxxxxx
```

### 2. Start infrastructure

```bash
make docker-up
make dev
make migrate
```

### 3. Start the server

```bash
make run
```

### 4. Expose with ngrok (new terminal)

```bash
ngrok http 8090
```

### 5. Run setup script

```bash
python examples/ab-testing/setup.py \
  --twilio-number "+15559876543" \
  --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  --server-url "https://xxxx.ngrok-free.app"
```

### 6. Call your number!

Call multiple times. You'll randomly get one of two styles:

| Version | Style | First message |
|---------|-------|---------------|
| v1 | Concise, direct | "TechCorp support. How can I help?" |
| v2 | Warm, friendly | "Hey there! Welcome to TechCorp support! ..." |

Same phone number always gets the same variant (deterministic by caller number).

## Versioning Workflow

```
POST /agents           → v1 (draft)
PUT  /agents/v1        → edit draft
POST /agents/v1/publish → v1 live, phone numbers routed here

POST /agents           → v2 (draft, same name)
POST /agents/v2/publish → v2 live, v1 auto-archived, phone numbers auto-promoted

GET  /agents/v2/versions → [v2 (published), v1 (archived)]
POST /agents/v1/rollback → v1 restored, v2 archived, phone numbers updated
```

## A/B Testing Workflow

```
# Set up weighted routing (50/50)
PUT /phone-numbers/{id}/routing
{"weights": [{"agent_id": "v1-id", "weight": 50}, {"agent_id": "v2-id", "weight": 50}]}

# Check routing config
GET /phone-numbers/{id}/routing
→ {"mode": "weighted", "weights": [...]}

# Adjust split (80/20)
PUT /phone-numbers/{id}/routing
{"weights": [{"agent_id": "v1-id", "weight": 80}, {"agent_id": "v2-id", "weight": 20}]}

# Conclude test (pick winner)
DELETE /phone-numbers/{id}/routing
→ Reverts to single-agent routing
```

## How Weighted Routing Works (Multi-Pod Safe)

```
Inbound call → phone number → routing_weights set?
  → YES: SHA256(caller_number) mod 100 → pick agent by weight
  → NO:  use routing_target_id (single agent)

Pod 1 computes: SHA256("+14155551234") mod 100 = 37 → v1 (0-49)
Pod 2 computes: SHA256("+14155551234") mod 100 = 37 → v1 (same!)
Pod 3 computes: SHA256("+14155551234") mod 100 = 37 → v1 (same!)
```

No Redis, no pod coordination, no state. Pure function over shared DB config.

## Architecture

```
Phone Call → Twilio → POST /webhooks/twilio/voice/inbound
  → Look up phone number → Check routing_weights
  → routing_weights set? → SHA256(caller) mod 100 → pick agent
  → routing_weights null? → use routing_target_id directly
  → Load agent config → Start Pipecat pipeline
  → STT (Cartesia Ink) → LLM (OpenAI) → TTS (Cartesia Sonic) → Audio
```

## Quick run

```bash
./run.sh
```

Reads `TURNCALL_NUMBER`, `TWILIO_PN_SID`, `PUBLIC_BASE_URL` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
