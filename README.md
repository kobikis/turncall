<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/wordmark-dark.svg">
    <img src="docs/logo/wordmark-light.svg" alt="TurnCall" width="360">
  </picture>

  <h3>Voice AI infrastructure you can self-host</h3>

  <p>
    Give an AI agent a phone number and let people call it.<br>
    Your keys, your servers, your models — no per-minute platform tax.
  </p>

  <p>
    <a href="https://github.com/kobikis/turncall/releases/latest"><img src="https://img.shields.io/github/v/release/kobikis/turncall?style=flat&color=blue" alt="Release"></a>
    <a href="https://github.com/kobikis/turncall/actions/workflows/ci.yml"><img src="https://github.com/kobikis/turncall/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat" alt="MIT"></a>
    <a href="#"><img src="https://img.shields.io/badge/python-3.12+-blue?style=flat&logo=python&logoColor=white" alt="Python 3.12+"></a>
    <a href="https://github.com/kobikis/turncall/pkgs/container/turncall"><img src="https://img.shields.io/badge/ghcr.io-turncall-2496ED?style=flat&logo=docker&logoColor=white" alt="Container image"></a>
    <a href="QUICKSTART.md"><img src="https://img.shields.io/badge/quickstart-10_min-0B7285?style=flat&logo=rocket&logoColor=white" alt="Quickstart"></a>
    <a href="https://github.com/kobikis/turncall/stargazers"><img src="https://img.shields.io/github/stars/kobikis/turncall?style=flat&color=yellow" alt="Stars"></a>
  </p>

  <p>
    <a href="#see-it">See it</a> ·
    <a href="QUICKSTART.md">Quick start</a> ·
    <a href="#features">Features</a> ·
    <a href="#api">API</a> ·
    <a href="#architecture">Architecture</a> ·
    <a href="https://docs.turncall.io">Docs</a> ·
    <a href="CONTRIBUTING.md">Contributing</a> ·
    <a href="#license">License</a>
  </p>
</div>

---

## See it

A receptionist that answers a real phone number. That's the whole thing:

```bash
curl -X POST http://localhost:8090/v1/agents -H "Authorization: Bearer tc_..." -d '{
  "name": "receptionist",
  "config": {
    "first_message": "Thanks for calling Acme — how can I help?",
    "system_prompt": "You are Acme'"'"'s receptionist. Be brief and warm.",
    "llm":  {"provider": "openai",   "model": "gpt-4o-mini"},
    "stt":  {"provider": "deepgram", "model": "nova-3-general"},
    "tts":  {"provider": "cartesia", "model": "sonic-3.5"},
    "tools": [{"name": "transfer_call"}]
  }
}'
```

Bind it to a Twilio number and call. Roughly **800 ms** to first word on the
cascade pipeline, or **~300 ms** with speech-to-speech.

Swap any provider by changing one line — the pipeline is the same. Run the LLM
locally through Ollama and no audio leaves your network except to the telco.

<table>
<tr>
<th width="50%">Most voice platforms</th>
<th width="50%">TurnCall</th>
</tr>
<tr>
<td>

- Per-minute pricing on top of provider costs
- Their model choices, their infrastructure
- Your call recordings on their servers
- Prompt and transcript data you don't control

</td>
<td>

- Self-hosted; you pay providers directly
- Any STT/LLM/TTS mix, per agent
- Recordings on your disk or your S3
- Runs fully offline-capable with local models

</td>
</tr>
</table>

## Features

- **Twilio PSTN calls** — Inbound and outbound phone calls via Twilio
- **WebRTC browser calls** — Talk to agents from the browser
- **Video Avatar** — Optional HeyGen LiveAvatar (lip-synced video) on WebRTC cascade calls
- **Multi-provider STT/LLM/TTS** — Deepgram, OpenAI, Anthropic Claude, ElevenLabs, Cartesia, Ollama, OpenRouter, or any OpenAI-compatible endpoint (configurable per agent)
- **Bring Your Own Model** — Use local LLMs via Ollama or remote endpoints (Together AI, Groq, vLLM, etc.)
- **Speech-to-Speech** — Ultra-low latency (~300ms) via OpenAI Realtime or Gemini Live
- **Smart Turn Detection** — ML-based (SmartTurnV3) understands natural pauses
- **Barge-in** — Silero VAD lets users interrupt mid-speech
- **Voicemail Detection** — With retry backoff, beep detection, auto-message
- **Tool Calling** — Built-in (transfer, handoff, end_call, DTMF) + webhook tools + MCP servers
- **MCP Support** — Connect agents to MCP servers for auto-discovered tools (HTTP, SSE, stdio)
- **Live Call Control** — End, transfer, handoff, inject context via API
- **Server Events** — Bidirectional hooks (call-init, function-call, call-end)
- **Pre-Call Init** — Dynamic agent resolution via webhook on all transports (Twilio, WhatsApp, WebRTC)
- **Knowledge Base (RAG)** — Upload docs, three retrieval modes: prompt (full inject), auto (per-turn RAG), tool (LLM-decided)
- **Knowledge Context** — Inject runtime data (CRM, tickets) into system prompt at call start
- **Template Variables** — Personalize prompts per-call with `{{name}}`, `{{account_id}}`
- **Agent Versioning** — Publish immutable versions, auto-promote phone numbers, rollback with one API call
- **A/B Testing** — Weighted traffic routing on phone numbers (50/50, 80/20, etc.), deterministic by caller
- **Post-Call Analysis** — Automatic LLM-powered analysis: summary, success evaluation, sentiment, structured extraction, scoring rubric
- **Multi-tenant** — Project-scoped API keys with RBAC
- **Observability** — Call events, transcripts, tool invocations, webhook delivery

## Quick Start

```bash
# Clone and install
git clone https://github.com/kobikis/turncall.git
cd turncall
python -m venv .venv && source .venv/bin/activate
make dev

# Configure
cp env.example .env
# Edit .env with your TWILIO, OPENAI, DEEPGRAM keys

# Start
make docker-up    # Postgres + Redis + TurnCall API (:8090) + LocalStack
make migrate      # Create tables
# No AWS/S3? Use local filesystem storage instead (no LocalStack):
#   make docker-up-local && make migrate-local
# (make run = host-mode dev server with reload; stop the turncall container first — both bind :8090)

# Setup a receptionist (with ngrok for local dev)
ngrok http 8090
# Set TURNCALL_NUMBER, TWILIO_PN_SID, PUBLIC_BASE_URL in .env, then:
./examples/receptionist/run.sh

# Call your number!
```

See [QUICKSTART.md](QUICKSTART.md) for detailed setup instructions.

## Architecture

```
Phone Call → Twilio → TurnCall webhook → TwiML with <Stream>
  → WebSocket → Pipecat Pipeline:
    Cascade: STT → VAD+SmartTurn → [KB Retrieval] → LLM → TTS → Audio back (~800ms)
    S2S:     VAD → OpenAI Realtime / Gemini Live → Audio back (~300ms)

Browser → POST /v1/webrtc/connect (SDP offer/answer) + PATCH (ICE trickle) → WebRTC audio → Same pipeline
```

Full system design: **[docs.turncall.io/architecture](https://docs.turncall.io/architecture)**.

## API

```bash
# Create project + API key
curl -X POST http://localhost:8090/v1/projects -d '{"name": "my-project"}'
curl -X POST "http://localhost:8090/v1/api-keys?project_id=UUID" -d '{"name": "dev", "role": "admin"}'

# Create agent
curl -X POST http://localhost:8090/v1/agents \
  -H "Authorization: Bearer tc_..." \
  -d '{"name": "receptionist", "config": {"system_prompt": "You are a helpful receptionist."}}'

# Bind phone number
curl -X POST http://localhost:8090/v1/phone-numbers \
  -H "Authorization: Bearer tc_..." \
  -d '{"external_number_sid": "PNxxx", "e164_number": "+1555...", "routing_target_type": "agent", "routing_target_id": "UUID"}'
```

Every endpoint: **[docs.turncall.io/api-reference](https://docs.turncall.io/api-reference/overview)**.

## Agent Config

```json
{
  "name": "receptionist",
  "config": {
    "system_prompt": "You are a dental clinic receptionist...",
    "first_message": "Thank you for calling! How can I help?",
    "stt": {"provider": "deepgram", "model": "nova-3-general"},
    "llm": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.7, "max_tokens": 1024},
    "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
    "tools": [
      {"name": "transfer_call", "description": "Transfer to agent", "parameters_schema": {...}},
      {"name": "lookup_customer", "webhook_url": "https://your-api.com/lookup", ...}
    ],
    "smart_turn_detection": true,
    "voicemail_detection": {"enabled": true, "voicemail_message": "Please call us back..."}
  }
}
```

### Anthropic Claude

Use Claude models for deeper reasoning:

```json
"llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}
```

### Bring Your Own Model (BYOM)

Use a local LLM via Ollama or any OpenAI-compatible endpoint:

```json
// Local Ollama
"llm": {"provider": "ollama", "model": "gemma3:12b"}

// Remote endpoint (Together AI, Groq, vLLM, etc.)
"llm": {
  "provider": "custom_openai",
  "model": "meta-llama/Llama-3-70b",
  "base_url": "https://api.together.xyz/v1",
  "api_key": "your-key"
}
```

See [examples/ollama-local/](examples/ollama-local/) for a complete setup guide.

### OpenRouter (model fallback routing)

Route through [OpenRouter](https://openrouter.ai) with automatic failover — if the
primary model rate-limits or errors mid-call, it falls over to the next model in order.
Set `OPENROUTER_API_KEY` and use the `openrouter` provider:

```json
"llm": {
  "provider": "openrouter",
  "model": "anthropic/claude-3.5-sonnet",
  "fallback_models": ["openai/gpt-4o", "google/gemini-flash-1.5"]
}
```

Voice only (WebRTC / Twilio / WhatsApp voice). The model that actually answered each
turn is recorded on `transcript.final` events. See
[examples/openrouter-fallback/](examples/openrouter-fallback/).

### Speech-to-Speech (S2S)

Skip STT/TTS entirely — the model handles audio natively:

```json
// OpenAI Realtime (~300ms latency)
"pipeline_mode": "s2s",
"s2s": {"provider": "openai", "voice": "alloy"}

// Gemini Live (cheaper, emotion-aware)
"pipeline_mode": "s2s",
"s2s": {"provider": "google", "voice": "Kore"}
```

Grok voice works too — the `openai` provider plus a gateway `base_url`
(the example's `--provider xai` presets it).

See [examples/s2s-realtime/](examples/s2s-realtime/) for setup.

### Video Avatar (HeyGen / Tavus)

Add a lip-synced video avatar to a WebRTC cascade agent. Renders in the browser alongside the voice. **WebRTC + cascade only** — the avatar lip-syncs the TTS audio; phone/S2S aren't supported.

```json
// HeyGen LiveAvatar
"avatar": {"enabled": true, "provider": "heygen", "avatar_id": "<id>", "is_sandbox": true}

// Tavus — higher fidelity, lower latency
"avatar": {"enabled": true, "provider": "tavus", "replica_id": "<id>"}
```
(with `"transport": "webrtc"`, `"pipeline_mode": "cascade"`)

- **HeyGen**: `HEYGEN_LIVE_AVATAR_API_KEY` — a **LiveAvatar** key from [app.liveavatar.com](https://app.liveavatar.com), **not** a HeyGen key (Pipecat targets the LiveAvatar API; HeyGen's old streaming API is sunset).
- **Tavus**: `TAVUS_API_KEY` from [platform.tavus.io](https://platform.tavus.io) + a `replica_id`. `persona_id` defaults to `pipecat-stream` (lip-syncs your TTS). Sub-600ms latency, 1080p — the better quality/latency option.

See [examples/video-avatar/](examples/video-avatar/) for setup.

### Knowledge Base (RAG)

Upload documents and attach them to agents with three retrieval modes:

```json
// 1. Create a knowledge base
POST /v1/knowledge-bases
{"name": "product-docs", "description": "Product documentation"}

// 2. Upload a document (multipart form)
POST /v1/knowledge-bases/{kb_id}/documents
// File: product-catalog.pdf

// 3. Link to agent with retrieval mode
POST /v1/agents/{agent_id}/knowledge-bases
{
  "knowledge_base_id": "kb-uuid",
  "mode": "auto",           // "prompt" | "auto" | "tool"
  "top_k": 5,
  "similarity_threshold": 0.7
}
```

| Mode | Behavior |
|------|----------|
| `prompt` | Full text injected into system prompt (best for small docs) |
| `auto` | Per-turn semantic search via pgvector (best for catalogs, FAQs) |
| `tool` | LLM calls `query_knowledge` when it needs info (best for large docs) |

Supports PDF, TXT, Markdown, DOCX, CSV, JSON, YAML. Requires PostgreSQL with `pgvector` extension.

See [examples/knowledge-base/](examples/knowledge-base/) for a complete setup demonstrating all three modes.

## Documentation

**[docs.turncall.io](https://docs.turncall.io)** — the full documentation, rendered.

| Doc | Description |
|-----|-------------|
| [QUICKSTART.md](QUICKSTART.md) | Step-by-step setup — browser first, no phone number needed |
| [Architecture](https://docs.turncall.io/architecture) | System design, pipeline, modules |
| [API reference](https://docs.turncall.io/api-reference/overview) | Every endpoint |
| [Providers](https://docs.turncall.io/guides/providers) | Choosing STT / LLM / TTS |
| [Server events](https://docs.turncall.io/guides/server-events) | Bidirectional hooks |
| [Knowledge base](https://docs.turncall.io/guides/knowledge-base) | RAG, three retrieval modes |
| [QUICKSTART.md](QUICKSTART.md) | The same setup, in-repo |

## Tools

TurnCall supports built-in tools and custom webhook tools per-agent.

### Built-in Tools (no server needed)

| Tool | Description |
|------|-------------|
| `end_call` | Terminate the call |
| `transfer_call` | Transfer to a phone number (warm or cold) |
| `handoff_to_agent` | Switch to another AI agent mid-call |
| `send_dtmf` | Send keypad tones for IVR navigation |

### Custom Webhook Tools

Define any tool with a `webhook_url` — TurnCall POSTs to your server when the LLM invokes it:

```json
{
  "name": "lookup_customer",
  "description": "Look up customer details by phone number",
  "parameters_schema": {
    "type": "object",
    "properties": {
      "phone_number": {"type": "string", "description": "E.164 phone number"}
    },
    "required": ["phone_number"]
  },
  "webhook_url": "https://your-api.com/turncall/tools/lookup-customer",
  "timeout_seconds": 15
}
```

Your server receives:
```json
{"tool_name": "lookup_customer", "arguments": {"phone_number": "+1555..."}, "call_id": "...", "project_id": "..."}
```

See [examples/tools-showcase/](examples/tools-showcase/) for a complete example with all tool types.

## Pre-Call Initialization (call-init)

Dynamically select and configure the agent before a call starts.

### Setup

Bind your phone number with `routing_target_type: "webhook"`:

```bash
curl -X POST http://localhost:8090/v1/phone-numbers \
  -H "Authorization: Bearer tc_..." \
  -d '{
    "e164_number": "+15551234567",
    "external_number_sid": "PNxxx",
    "routing_target_type": "webhook",
    "server_url": "https://your-server.com/turncall/init"
  }'
```

For WebRTC, pass `server_url` in requestData (no phone number needed):
```json
POST /v1/webrtc/connect
{"sdp": "v=0...", "type": "offer", "requestData": {"server_url": "https://your-server.com/turncall/init"}}
```

### Your Server Receives

```json
{
  "message": {
    "type": "call-init",
    "phoneNumber": {"number": "+15551234567"},
    "customer": {"number": "+15559876543"},
    "call": {"id": "temp-uuid", "provider_call_id": "CA...", "type": "inboundPhoneCall"}
  }
}
```

Transport types: `inboundPhoneCall`, `inboundWhatsAppCall`, `webrtc`

### Your Server Responds

```json
{
  "agent_id": "uuid-of-existing-agent",
  "variables": {"customer_name": "Jane", "tier": "premium"},
  "metadata": {"crm_id": "C-123"},
  "dynamic_data": {
    "knowledge_context": "Customer has open ticket #456 about billing."
  }
}
```

Or return inline config:
```json
{
  "agent": {"system_prompt": "...", "llm": {"provider": "openai"}, ...},
  "variables": {"name": "Jane"}
}
```

### What Happens

1. `call.initializing` webhook event fires (informational)
2. Template `{{variables}}` are rendered into the system prompt
3. `knowledge_context` is prepended to the system prompt
4. `metadata` is stored on the call record
5. Pipeline starts with the resolved configuration

## Examples

Every example ships a `run.sh` launcher: it reads the shared values
(`TURNCALL_NUMBER`, `TWILIO_PN_SID`, `PUBLIC_BASE_URL`) from `.env` and passes
any extra flags through to its `setup.py` — see each example's README.

| Example | Description |
|---------|-------------|
| [examples/receptionist/](examples/receptionist/) | Dental clinic receptionist with transfer + handoff |
| [examples/call-transfer/](examples/call-transfer/) | Cold/warm transfer to a human with caller message + operator briefing |
| [examples/tools-showcase/](examples/tools-showcase/) | All tool types: built-in, webhook, call-init, knowledge context |
| [examples/ollama-local/](examples/ollama-local/) | Voice agent with local LLM via Ollama (BYOM) |
| [examples/openrouter-fallback/](examples/openrouter-fallback/) | OpenRouter LLM with model fallback routing (voice only) |
| [examples/s2s-realtime/](examples/s2s-realtime/) | Ultra-low latency S2S with OpenAI Realtime / Gemini Live / Grok (via gateway) |
| [examples/webrtc-client/](examples/webrtc-client/) | Browser voice client (Pipecat JS + WebRTC) |
| [examples/video-avatar/](examples/video-avatar/) | WebRTC cascade agent with a HeyGen LiveAvatar video |
| [examples/knowledge-base/](examples/knowledge-base/) | Knowledge base RAG with all three retrieval modes |
| [examples/sms-chat/](examples/sms-chat/) | SMS and Chat API text conversations |
| [examples/ab-testing/](examples/ab-testing/) | Agent versioning + A/B testing with weighted routing |
| [examples/whatsapp/](examples/whatsapp/) | WhatsApp Business voice + text |
| [examples/events-webhook/](examples/events-webhook/) | Webhook event subscriptions with a local receiver server |
| [examples/mcp-tools/](examples/mcp-tools/) | Agent tools auto-discovered from an MCP server |
| [examples/post-call-analysis/](examples/post-call-analysis/) | Post-call summary, sentiment, and structured extraction |

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | FastAPI + Uvicorn |
| Voice Pipeline | Pipecat 1.8 |
| Database | PostgreSQL + SQLAlchemy async + Alembic |
| Cache | Redis |
| Telephony | Twilio Voice + Media Streams |
| Browser | WebRTC (SmallWebRTCTransport) |
| Video Avatar | HeyGen LiveAvatar (Pipecat HeyGenVideoService) |
| STT | Deepgram / OpenAI / ElevenLabs / Cartesia |
| LLM | OpenAI / Anthropic Claude / Ollama / Any OpenAI-compatible |
| TTS | Deepgram / OpenAI / ElevenLabs / Cartesia |
| S2S | OpenAI Realtime / Gemini Live |
| Knowledge Base | pgvector + OpenAI embeddings |
| VAD | Silero |
| Turn Detection | Smart Turn V3 (local ONNX) |
| Logging | Loguru |

## Contributing

Bug fixes and docs: open a PR. Features: [open an issue
first](https://github.com/kobikis/turncall/issues/new/choose) so we can agree on
the approach before you write code.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the DCO sign-off requirement,
and an honest account of what CI does and doesn't enforce. By participating you
agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Please don't file public issues for vulnerabilities — see
[SECURITY.md](SECURITY.md) for private reporting.

Two settings matter most if you self-host: set `API_KEY_HASH_SECRET` to a strong
unique value, and change `PLATFORM_API_KEY` from the shipped dev default.

## License

MIT — see [LICENSE](LICENSE).

TurnCall is open core. This engine is MIT. The separate agent-builder repos are
released under FSL-1.1-Apache-2.0 (converting to Apache 2.0 after two years);
see [adr/0015](adr/0015-open-core-licensing.md) for the reasoning.
