# OpenRouter Fallback Routing Example

A voice agent routed through [OpenRouter](https://openrouter.ai) with **model
fallback routing**: if the primary model rate-limits or errors mid-call,
OpenRouter automatically fails over to the next model in your list, in order.

## What it does

- Runs a voice agent on the `openrouter` provider with a primary model plus an
  ordered `fallback_models` chain
- Demonstrates OpenRouter's `models` array (failover) end-to-end
- Records **which model actually answered** each turn (on `transcript.final` events)

## Constraints

- **Voice only** (WebRTC / Twilio / WhatsApp voice). OpenRouter is **not** supported
  on the SMS/Chat text path — that's a deliberate v1 scope decision
  (see [ADR-0003](../../adr/0003-openrouter-provider.md)).
- `fallback_models` is only valid when `provider` is `openrouter`; setting it on any
  other provider is rejected at agent-create time.

## Prerequisites

1. An **OpenRouter API key**: https://openrouter.ai/keys
2. **Deepgram API key** (for STT/TTS — free tier available)
3. **Docker** (for Postgres + Redis)
4. **ngrok** (to expose local server to Twilio, if using phone calls)

## Quick Start

### 1. Configure environment

```bash
cp env.example .env
# Edit .env:
#   OPENROUTER_API_KEY=sk-or-...   (platform-level key, shared by all agents)
#   DEEPGRAM_API_KEY=your-deepgram-key
```

The server reads `OPENROUTER_API_KEY` once at startup. Individual agents can override
it with a per-agent key via `--llm-api-key`.

### 2. Start infrastructure

```bash
make docker-up    # postgres + redis
make migrate      # create database tables
make run          # start server
```

### 3. Run setup script

```bash
# WebRTC only (browser calls, no Twilio needed):
python examples/openrouter-fallback/setup.py --server-url "http://localhost:8090"

# Custom primary + fallback chain:
python examples/openrouter-fallback/setup.py \
  --server-url "http://localhost:8090" \
  --llm-model "anthropic/claude-3.5-sonnet" \
  --fallback "openai/gpt-4o" --fallback "google/gemini-flash-1.5"

# With Twilio phone number:
python examples/openrouter-fallback/setup.py \
  --server-url "https://xxxx.ngrok.io" \
  --twilio-number "+15559876543" \
  --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### 4. Talk to it!

- **Browser**: Open the WebRTC client at `examples/webrtc-client/index.html`
- **Phone**: Call your Twilio number

## Agent config

The only OpenRouter-specific part is the `llm` block:

```json
{
  "provider": "openrouter",
  "model": "anthropic/claude-3.5-sonnet",
  "fallback_models": ["openai/gpt-4o", "google/gemini-flash-1.5"]
}
```

This becomes OpenRouter's request-body `models` array, primary first:
`["anthropic/claude-3.5-sonnet", "openai/gpt-4o", "google/gemini-flash-1.5"]`.
OpenRouter tries them in order and returns the first that succeeds.

## Seeing which model answered

OpenRouter reports the model that actually handled each response. TurnCall captures it
and writes it to the assistant's `transcript.final` events:

```json
{ "text": "...the assistant's reply...", "user_id": "assistant", "model": "openai/gpt-4o" }
```

So when the primary fails over, `payload.model` tells you what answered. Pull it from
the call's transcript/events.

## Architecture

```
Phone/Browser → Transport → Audio → STT (Deepgram cloud)
  → LLM (OpenRouter @ openrouter.ai, primary → fallbacks)
  → TTS (Deepgram cloud) → Audio
```

## Quick run

```bash
./run.sh
```

Reads `PUBLIC_BASE_URL` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
