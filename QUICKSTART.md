# Quickstart

Get a voice AI receptionist answering a real phone call.

Budget 20–30 minutes the first time. Most of it is signing up for Twilio
and the provider keys — the stack itself starts in one command.

## Prerequisites

- Docker (runs the whole stack — Postgres, Redis, API)
- Python 3.12+ (only for [host development](#host-development-optional): hot-reload, tests, scripts)
- A Twilio account with a phone number
- API keys for: OpenAI (or Ollama for local LLM), Deepgram (free tier), and optionally ElevenLabs or Cartesia
- ngrok (for local development)

## 1. Get the Code

```bash
git clone https://github.com/kobikis/turncall.git
cd turncall
```

Everything below runs in Docker — no host Python setup needed. (If you want to
run the server on your host for hot-reload, see [Host Development](#host-development-optional).)

## 2. Configure

```bash
cp env.example .env
```

Edit `.env`:

```
DATABASE_URL=postgresql+asyncpg://turncall:turncall@localhost:5432/turncall
REDIS_URL=redis://localhost:6379/0
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxx
DEEPGRAM_API_KEY=xxxxxxxx
ELEVENLABS_API_KEY=xxxxxxxx    # Optional
```

Project/API-key creation (the bootstrap endpoints) requires the `X-Platform-Key`
header matching `PLATFORM_API_KEY`. The `env.example` dev default
(`dev-platform-key`) is read from `.env` automatically by the example setup
scripts and `scripts/seed_dev.sh` — set a strong unique value in production.

Get free API keys:
- **Deepgram**: [console.deepgram.com](https://console.deepgram.com) (free $200 credit)
- **ElevenLabs**: [elevenlabs.io](https://elevenlabs.io) (free tier)

## 3. Start Everything

This one command builds and runs the whole stack — Postgres, Redis, and the
TurnCall API on `http://localhost:8090`:

```bash
make docker-up    # Postgres + Redis + TurnCall API (:8090) + LocalStack (S3)
make migrate      # Creates database tables
```

Don't need S3? Skip LocalStack and use local filesystem storage
(`STORAGE_BACKEND=local`, writes to `./storage`):

```bash
make docker-up-local    # Postgres + Redis + TurnCall API (:8090), no LocalStack
make migrate-local      # Creates database tables
```

That's it — the API is live. Continue to step 4 to point Twilio at it.

> **Which one you chose decides your container names.** The compose project is
> named after its directory, so `make docker-up` gives you `localstack-turncall-1`
> and `localstack-postgres-1`, while `make docker-up-local` gives you
> `turncall-local-*`. Confusing, but harmless on its own — it matters if you also
> run [TurnCall Builder](https://github.com/kobikis/turncall-builder-api), which
> reaches into these containers by name and has a matching target for each.
> `docker ps` tells you which you have.

## 4. Expose via ngrok

In a new terminal:

```bash
ngrok http 8090
```

Copy the `https://xxxx.ngrok-free.app` URL.

## 5. Run the Setup Script

Find your Twilio Phone Number SID in the [Twilio Console](https://console.twilio.com) under Phone Numbers → Active Numbers.

Set the shared values once in `.env`, then use the example's launcher:

```bash
# in .env: TURNCALL_NUMBER=+15559876543  TWILIO_PN_SID=PNxxxxxxxx  PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
./examples/receptionist/run.sh
```

(Every example has a `run.sh`; extra flags pass through to its `setup.py`.
The manual equivalent:)

```bash
python examples/receptionist/setup.py \
  --twilio-number "+15559876543" \
  --twilio-number-sid "PNxxxxxxxx" \
  --server-url "https://xxxx.ngrok-free.app"
```

This creates:
- A project
- An API key (save it!)
- A billing agent
- A receptionist agent with tools (transfer, handoff, end_call)
- Binds your Twilio number to the receptionist
- Configures Twilio webhooks

## 6. Call Your Number

Try these scenarios:

| You say | What happens |
|---------|-------------|
| "I need to schedule an appointment" | Transfers to front desk (human) |
| "I have a billing question" | Hands off to billing AI agent |
| "What are your office hours?" | Answers directly |

## Host Development (optional)

The Docker path above needs no host Python. You only need a local environment to
**run code on your host** — hot-reload server, tests, lint, or seed/eval scripts:

```bash
python -m venv .venv && source .venv/bin/activate
make dev          # pip install -e ".[dev]"
```

Then, with the infra containers up, run the server on the host with reload
(stop the `turncall` container first — both bind `:8090`):

```bash
make run          # host-mode dev server with reload
make test         # pytest with coverage
make lint         # ruff
```

## What's Running

```
You call → Twilio → POST /webhooks/twilio/voice/inbound
  → Creates call record → Returns TwiML
  → Twilio opens WebSocket → Pipecat pipeline starts
  Cascade: → VAD → STT (Deepgram) → SmartTurn → LLM (OpenAI/Ollama) → TTS → You hear response
  S2S:     → Audio → OpenAI Realtime or Gemini Live → You hear response (faster!)
```

## Browser Calls (WebRTC)

You can also talk to agents from the browser:

```bash
cd examples/webrtc-client
npm install
npm run dev
```

Open `http://localhost:5173`, enter your API key and agent ID, click Start Call.

## Monitoring

```bash
# List calls
curl http://localhost:8090/v1/calls -H "Authorization: Bearer tc_xxx"

# Get transcript
curl http://localhost:8090/v1/calls/CALL_ID/transcript -H "Authorization: Bearer tc_xxx"

# Get events (transfers, tool calls)
curl http://localhost:8090/v1/calls/CALL_ID/events -H "Authorization: Bearer tc_xxx"

# Get tool invocations
curl http://localhost:8090/v1/tools/invocations/CALL_ID -H "Authorization: Bearer tc_xxx"
```

## Next Steps

Full documentation, rendered: **[docs.turncall.io](https://docs.turncall.io)**

- [API reference](https://docs.turncall.io/api-reference/overview) — every endpoint
- [Architecture](https://docs.turncall.io/architecture) — system design and the pipeline
- [Server events](https://docs.turncall.io/guides/server-events) — dynamic routing with hooks
- [Providers](https://docs.turncall.io/guides/providers) — choosing STT / LLM / TTS

### Build agents by describing them

[TurnCall Builder](https://github.com/kobikis/turncall-builder-api) is a
conversational agent builder for this engine: describe an agent in plain
English, answer a few follow-up questions, and it generates and creates the
config for you. It runs against the stack you just started.

Two things to know before you wire it up:

- **`PLATFORM_API_KEY` must be identical in both repos.** The builder mints its
  TurnCall API key through the platform-gated bootstrap endpoints, so its
  `.env` value has to match the one here. A mismatch fails with a 401.
- **Use the builder target that matches how you started TurnCall** —
  `make docker-up-local` here pairs with `make docker-up-local` there, and
  `make docker-up` with `make docker-up`, because the container names differ.

## Customization

### Change providers

```json
{
  "stt": {"provider": "deepgram", "model": "nova-3-general"},
  "llm": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.7, "max_tokens": 1024},
  "tts": {"provider": "elevenlabs", "voice": "your-voice-id"}
}
```

Supported STT/TTS: `deepgram`, `openai`, `elevenlabs`, `cartesia`
Supported LLM: `openai`, `anthropic`, `ollama`, `custom_openai`, `openrouter`

### Sampling

`llm.temperature` (0–2, default 0.7) and `llm.max_tokens` (default 1024) apply on
voice calls and chat/SMS alike. S2S agents use `s2s.max_tokens` (both providers)
and `s2s.temperature` (Gemini Live only — the OpenAI Realtime GA API has no
temperature and rejects it with a 422); unset means the provider's default.

### Use a local model (Ollama)

```json
{
  "llm": {"provider": "ollama", "model": "gemma3:12b"}
}
```

Or a remote OpenAI-compatible endpoint:

```json
{
  "llm": {
    "provider": "custom_openai",
    "model": "meta-llama/Llama-3-70b",
    "base_url": "https://api.together.xyz/v1",
    "api_key": "your-key"
  }
}
```

See [examples/ollama-local/](examples/ollama-local/) for a full walkthrough.

### Use OpenRouter with model fallback

Set `OPENROUTER_API_KEY` in `.env`, then route through OpenRouter with automatic
failover (voice only):

```json
{
  "llm": {
    "provider": "openrouter",
    "model": "anthropic/claude-3.5-sonnet",
    "fallback_models": ["openai/gpt-4o", "google/gemini-flash-1.5"]
  }
}
```

If the primary rate-limits or errors mid-call, OpenRouter falls over to the next model
in order. See [examples/openrouter-fallback/](examples/openrouter-fallback/).

### Speech-to-Speech (ultra-low latency)

Skip STT/TTS entirely — the model handles audio natively (~300ms vs ~800ms):

```json
{
  "pipeline_mode": "s2s",
  "s2s": {"provider": "openai", "voice": "alloy"}
}
```

Or with Gemini Live (cheaper):

```json
{
  "pipeline_mode": "s2s",
  "s2s": {"provider": "google", "voice": "Kore"}
}
```

See [examples/s2s-realtime/](examples/s2s-realtime/) for a full walkthrough.

### Video avatar (HeyGen / Tavus)

Add a lip-synced video avatar to a WebRTC cascade agent (avatar renders in the
`webrtc-client` automatically once a video track arrives):

```json
// HeyGen
{"transport": "webrtc", "pipeline_mode": "cascade",
 "avatar": {"enabled": true, "provider": "heygen", "avatar_id": "<id>", "is_sandbox": true}}

// Tavus — higher quality, lower latency
{"transport": "webrtc", "pipeline_mode": "cascade",
 "avatar": {"enabled": true, "provider": "tavus", "replica_id": "<id>"}}
```

- **HeyGen**: `HEYGEN_LIVE_AVATAR_API_KEY` — a **LiveAvatar** key from [app.liveavatar.com](https://app.liveavatar.com) (not a HeyGen key).
- **Tavus**: `TAVUS_API_KEY` from [platform.tavus.io](https://platform.tavus.io) + a `replica_id`; run `pip install -e .` first for the `tavus` extra (`daily-python`).

WebRTC + cascade only. Run it:

```bash
python examples/video-avatar/setup.py --avatar-id <id>                 # HeyGen
python examples/video-avatar/setup.py --provider tavus --replica-id <id>  # Tavus
```

See [examples/video-avatar/](examples/video-avatar/).

### Add tools

```json
{
  "tools": [{
    "name": "lookup_customer",
    "description": "Look up customer by phone number",
    "parameters_schema": {"type": "object", "properties": {"phone": {"type": "string"}}},
    "webhook_url": "https://your-api.com/lookup"
  }]
}
```

### Template variables

```json
{"system_prompt": "You are helping {{customer_name}} with account {{account_id}}."}
```

Server responds to `call-init` with:
```json
{"agent_id": "...", "variables": {"customer_name": "John", "account_id": "123"}}
```

### Voicemail detection (outbound)

```json
{
  "voicemail_detection": {
    "enabled": true,
    "voicemail_message": "Hi, please call us back at 555-1234.",
    "backoff_plan": {"max_retries": 3, "start_at_seconds": 5, "frequency_seconds": 3}
  }
}
```
