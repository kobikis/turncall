# TurnCall

Production voice agent platform. API-only backend for real-time AI voice agents over Twilio PSTN, WebRTC browser calls, WhatsApp Business (voice + text), and SMS/chat text conversations.

## Stack

- **Runtime**: Python 3.12, FastAPI, Pipecat 1.11
- **Database**: PostgreSQL (asyncpg + SQLAlchemy async + Alembic)
- **Cache**: Redis
- **Telephony**: Twilio Voice + Media Streams (WebSocket)
- **WhatsApp**: WhatsApp Cloud API + Pipecat WhatsAppClient (voice calls + text messages)
- **SMS/Chat**: Twilio SMS + Chat API (text conversations with session management)
- **WebRTC**: Pipecat SmallWebRTCTransport (browser calls)
- **Video Avatar**: HeyGen LiveAvatar via Pipecat HeyGenVideoService (WebRTC + cascade only)
- **STT**: Deepgram Nova-3 (streaming, the `nova-3-general` default) · OpenAI Whisper · ElevenLabs Scribe · Cartesia Ink
- **LLM**: OpenAI GPT-4o-mini · Anthropic Claude (Sonnet, Haiku, Opus) · Ollama (local models) · OpenRouter (multi-model + fallback routing) · Any OpenAI-compatible endpoint
- **S2S**: OpenAI Realtime · Gemini Live (native audio-in/audio-out)
- **TTS**: Deepgram Aura-2 · OpenAI TTS-1 · ElevenLabs Flash v2.5 · Cartesia Sonic-3.6
- **VAD**: Silero (barge-in / interruption handling)
- **Turn Detection**: Smart Turn V3 (ML-based, local ONNX)
- **Voicemail**: Pipecat VoicemailDetector with retry backoff
- **Knowledge Base**: pgvector (RAG), OpenAI embeddings, pypdf
- **Logging**: Loguru

## Quick Start

```bash
cp env.example .env        # Add TWILIO, OPENAI, DEEPGRAM, ELEVENLABS keys
make docker-up             # Start Postgres + Redis + TurnCall API (:8090) + LocalStack
make migrate               # Create database tables
make run                   # Host-mode dev server w/ reload (stop the turncall container first — both bind :8090)
./examples/<name>/run.sh   # Run any example (reads TURNCALL_NUMBER, TWILIO_PN_SID, PUBLIC_BASE_URL from .env)
```

## Project Structure

```
src/turncall/
  api/v1/          # REST API endpoints + Pydantic schemas (incl. /chat)
  auth/            # API key auth, RBAC, dependencies
  config/          # Settings (env vars via pydantic-settings + dotenv)
  domain/          # Enums, immutable models, call + session state machines
  events/          # Webhook delivery, server events, signing
  orchestrator/    # Pipecat pipeline: serializer, factory, session, tools, VAD, smart turn
  services/        # Call control, SMS/chat orchestration, WhatsApp chat, LLM text completion, template rendering, document ingestion, retrieval, weighted routing, post-call analysis
  storage/         # SQLAlchemy models, repositories, database/redis
  adapters/        # Object storage (local filesystem, S3)
  webhooks/        # Twilio handlers (voice + SMS), WhatsApp handlers (voice + text), media stream WS
```

## Key Commands

```bash
make run              # Dev server with reload
make test             # pytest with coverage (hermetic — excludes `live`)
make test-live        # real providers, needs credentials; skips what it lacks
make lint             # ruff check
make format           # ruff format
make migrate          # alembic upgrade head (runs in docker; make docker-up first)
make docker-up        # Postgres + Redis + TurnCall API + LocalStack
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Yes | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Yes | Twilio auth token |
| `OPENAI_API_KEY` | Yes* | OpenAI API key (LLM + optional STT/TTS). *Not required if using Ollama/custom LLM |
| `DEEPGRAM_API_KEY` | Yes | Deepgram API key (streaming STT/TTS) |
| `ELEVENLABS_API_KEY` | No | ElevenLabs API key (optional STT/TTS) |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `REDIS_URL` | Yes | Redis connection string |
| `BYOM_ENABLED` | No | Enable/disable BYOM custom providers (default: true) |
| `BYOM_ALLOWED_URL_PATTERNS` | No | JSON list of allowed base_url patterns for BYOM. A pattern naming a host must match the URL's **host** as well as the whole string — fnmatch's `*` spans `/`, so `https://*.trusted.com/*` used to accept `https://evil.com/x.trusted.com/y` |
| `MCP_MAX_TOOLS_TOTAL` | No | Ceiling on MCP tools across **all** servers (default `100`). `MCP_MAX_TOOLS_PER_SERVER` (default `50`) is per server and doesn't compose |
| `MCP_MAX_RESPONSE_BYTES` | No | Cap on one MCP tool result (default `1048576`). Over it, the model gets an error plus a 512-byte preview instead of the payload |
| `MCP_CONNECT_TIMEOUT_SECONDS` | No | Whole-discovery budget for connecting an agent's MCP servers (default `10`). Past it the call proceeds with **no** MCP tools rather than waiting — a caller is listening to silence, and stdio carries no timeout of its own |
| `TOOL_MAX_RESPONSE_BYTES` | No | The same cap for **custom webhook** tools (default `1048576`). Separate knob, identical behaviour — a tool result stays in the context for the rest of the conversation, so an oversized one is charged on every later turn |
| `GOOGLE_API_KEY` | No | Google API key (Gemini Live S2S) |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (Claude LLM). Not required if using other providers |
| `CARTESIA_API_KEY` | No | Cartesia API key (Sonic TTS + Ink STT). Not required if using other providers |
| `OPENROUTER_API_KEY` | No | OpenRouter API key (multi-model LLM + fallback routing). Platform-level key; per-agent `api_key` overrides. From openrouter.ai/keys |
| `HEYGEN_LIVE_AVATAR_API_KEY` | No | LiveAvatar API key for the HeyGen video avatar. From app.liveavatar.com (NOT a HeyGen key — Pipecat targets the LiveAvatar API) |
| `WHATSAPP_TOKEN` | No | WhatsApp API access token (Meta Developer Console > WhatsApp > API Setup) |
| `WHATSAPP_PHONE_NUMBER_ID` | No | WhatsApp Business phone number ID |
| `WHATSAPP_APP_SECRET` | No | Meta App Secret for webhook signature validation (App Settings > Basic > App Secret, 32 hex chars) |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | No | Arbitrary token for webhook URL verification handshake |
| `STORAGE_BACKEND` | No | Object storage backend: `local` (default) or `s3` |
| `LOCAL_STORAGE_PATH` | No | Local storage directory (default: `./storage`) |
| `S3_BUCKET_NAME` | No | S3 bucket for file storage (required if STORAGE_BACKEND=s3) |
| `AWS_REGION` | No | Default AWS region for S3 **and** the `bedrock`/`aws` providers (default: `us-east-1`). Per-agent `config.aws.region` overrides it — Bedrock model availability is region-specific and rarely matches your bucket. See `adr/0016` |
| `AWS_AGENT_CREDENTIALS_ENABLED` | No | Allow per-agent **static** AWS keys in the agent config (default `false`). They persist in `config_blob`, which is unencrypted JSONB, so agents supplying them are rejected at create unless this is on. The unrestricted path is `config.aws.role_arn`, which stores no durable secret. See `adr/0016` |
| `PUBLIC_BASE_URL` | No | Public https base URL (e.g. `https://abc.ngrok.io`) for Twilio callbacks issued without an inbound request — warm-transfer briefing + no-answer fallback. Cold transfer works without it. See `adr/0009` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | OTLP collector endpoint for OpenTelemetry traces (e.g. `http://localhost:4318`). Required for tracing in production (it self-disables without one). `OTEL_EXPORTER_OTLP_PROTOCOL` (`http/protobuf`\|`grpc`), `OTEL_EXPORTER_OTLP_HEADERS` also honored. See `adr/0010` |
| `PIPECAT_VAD_CONFIDENCE_THRESHOLD` | No | Silero VAD confidence (default `0.6`). Pairs with the agent's `silence_timeout_ms`, which sets the VAD stop window when Smart Turn is off (with it on, the model decides the turn and VAD uses Pipecat's 0.2s — the two waits are serial, so charging both cost 1.8s a turn) |
| `PIPECAT_ENABLE_OBSERVERS` / `PIPECAT_ENABLE_TRACING` / `PIPECAT_TRACE_INCLUDE_PII` | No | Observability toggles (all default `true`). PII = caller phone numbers on spans. See `adr/0010` |
| `API_KEY_HASH_SECRET` | Prod | Pepper for the HMAC-SHA256 hashing of API keys — a DB leak alone can't brute-force keys without it. **Set a strong value once and don't rotate** (rotating invalidates peppered keys; pre-pepper keys keep working via dual-read + upgrade-on-use). Default `change-me-in-production` gives no real protection until set |
| `EVAL_TTS_CACHE_DIR` | No | Where the caller's synthesized turns are cached in audio runs (default `./storage/eval-tts-cache`). Pipecat's own default lives under `$HOME`, which a container loses on recreate — every repeat run would then re-synthesize every caller turn. Mount it |
| `EVAL_MAX_CONCURRENT_RUNS` | No | Scenario-iterations the eval worker runs at once (default `4`). The worker runs the bot pipeline **and** the harness — which itself runs a persona LLM, a TTS, an STT and the judge — so in audio mode one run is roughly double a real call's service load in one event loop. A starting point, not a measurement. `EVAL_MAX_RUN_DURATION_SECONDS` (default `900`) is the janitor's cutoff for reclaiming a run a crashed worker left claimed, as `errored`; `EVAL_MAX_QUEUED_SECONDS` (`3600`) is the same cutoff for a run **nothing ever claimed** — the API committed the row then died before the queue push — and is longer, because waiting behind a backlog is normal where running for 15 minutes is not. `EVAL_JANITOR_INTERVAL_SECONDS` (`60`) bounds the sweep. One request's paid LLM work has **two** axes and needs both: `EVAL_MAX_ITERATIONS` (`50`) per run, and `EVAL_MAX_SCENARIOS_PER_REQUEST` (`50`) on a `tag`'s fan-out — a tag matching more than that is refused naming the count, never truncated, because a batch that ran 50 of 200 scenarios reports a verdict for a suite that never ran. See `adr/0018` |
| `EVAL_JUDGE_PROVIDER` | No | The LLM that decides every verdict, set once for the platform (#119) — one of `ollama` (pipecat's own, the default when nothing is set), `openai`, `anthropic`. With `EVAL_JUDGE_MODEL` (that provider's own default when empty) and `EVAL_JUDGE_TEMPERATURE` (unset by default — a verdict that varies run to run is not a verdict; dropped entirely for Anthropic and the OpenAI reasoning families, as on the call path). `EVAL_SIMULATOR_PROVIDER` / `_MODEL` / `_TEMPERATURE` are the same three for the LLM that plays the caller in a simulation. A **scenario's** own `judge:`/`simulator:` block wins, taken whole; a `judge` on a **run** request is a 422, since a run is what it was queued as |
| `PROJECT_PURGE_RETENTION_DAYS` | No | Days a soft-deleted project (ADR-0011) is kept before the hourly purge job hard-deletes it (cascade). Default `30`; `0` disables |
| `PLATFORM_API_KEY` | Prod | Privileged credential gating the unauthenticated bootstrap endpoints — project creation + first-API-key creation. Only the builder holds it; presented as the `X-Platform-Key` header. Empty default fails **closed** (rejects all bootstrap calls), so set it wherever those endpoints must work. TurnCall stays identity-free — this is a caller check, not a user |

## Providers (per-agent config)

| Role | Providers |
|------|-----------|
| STT | `deepgram` (default, streaming), `openai`, `elevenlabs`, `cartesia` (Ink, streaming). `stt.model` empty = that provider's own default (`nova-3-general` / `gpt-transcribe` / `scribe_v1` / `ink-whisper`), resolved in the pipeline factory the way `_tts_model_voice` already does for TTS. It used to default to `nova-3-general` for **every** provider, which the other three answer 400 to — so that value is read as unset on them, since `model_dump()` persisted it into existing agents. `stt.keyterms` is one list of vocabulary hints — product names, SKUs, surnames — mapped to each provider's own spelling: `keyterm` on Cartesia, `keywords` on OpenAI, `keyterms` on ElevenLabs, and on Deepgram **whichever the model takes** — `keyterm` for `nova-3*`/`flux*`, `keywords` for everything older. Deepgram 400s on the wrong one (`INVALID_QUERY_PARAMETER`) rather than ignoring it, so a hand-written `extra: {"keyterm": ...}` on a Nova-2 agent kills the call at connect; `keyterms` beats the same key in `extra` for that reason. Cartesia honors them on `ink-2*`/`ink-preview*` only and caps a connection at 100 terms / 1200 characters, truncating past that with a warning. Blanks and duplicates are dropped at the API boundary |
| LLM | `llm.model` empty = the provider's house model (`gpt-4o-mini` for openai, `claude-sonnet-5` for anthropic). `ollama`, `custom_openai`, `bedrock` and `openrouter` have none — the model *is* the deployment there — so an unnamed one is a 422 at create rather than a dead call. It used to default to `gpt-4o-mini` for **every** provider (Anthropic: `404 not_found_error`), and `model_dump()` persisted that into existing agents, so the value is read as unset off OpenAI. `openai` (default), `anthropic` (Claude), `ollama` (local), `custom_openai` (any OpenAI-compatible endpoint), `openrouter` (multi-model + `fallback_models` routing, voice only), `bedrock` (AWS-hosted Anthropic/Meta/Mistral/Amazon models — a *gateway*, not a vendor; credentials come from the agent's `aws` block, and `llm.extra` passes through to `additionalModelRequestFields` for e.g. Anthropic extended thinking. See `adr/0016`). Sampling: `llm.temperature` (0–2, default 0.7) + `llm.max_tokens` (default 1024) apply on voice and chat/SMS; the voicemail classifier stays pinned at 0.1. **Claude is sent no temperature at all, on either route** — `anthropic` direct and `anthropic.*` models via `bedrock`. Current Claude models reject it (`400 "temperature is deprecated for this model"`), which made the service unusable and ended the call. The deprecation belongs to the model, so it travels with the model rather than the endpoint; the other vendors `bedrock` fronts (Meta, Mistral, Amazon) still get one. `llm.extra` is the way to set one on an older model that still accepts it — e.g. `"extra": {"temperature": 0.3}`. The Anthropic SDK dropped `temperature`/`top_k`/`top_p` from `messages.create()`, so TurnCall routes any key it won't take by name into `extra_body` (checked against the SDK's own signature). Passing one directly used to raise `TypeError` on the first LLM turn. `llm.reasoning_effort` (`minimal`\|`low`\|`medium`\|`high`, unset by default) sent via `extra_body` on voice + chat/SMS — OpenAI-family only (openai/openrouter/custom_openai), for reasoning models (o-series/gpt-5); classifier forces it off. See `adr/0014` |
| TTS | `deepgram` (default), `openai`, `elevenlabs`, `cartesia` (Sonic-3.6, streaming, 60+ emotions) |
| S2S | `openai` (Realtime API), `openai_live` (GPT-Live-1, full duplex), `google` (Gemini Live, `gemini-3.8-live`; `voice` defaults to `Charon`), `aws` (Amazon Nova Sonic 2, `amazon.nova-2-sonic-v1:0`; `voice` defaults to `matthew`, `s2s.extra.endpointing_sensitivity` = `LOW`\|`MEDIUM`\|`HIGH`. Sessions roll over every ~6 min and credentials are re-resolved each time. See `adr/0016`) — set `pipeline_mode: "s2s"`. Sampling: `s2s.max_tokens` (both), `s2s.temperature` (google only — Realtime GA rejects it with 422). `openai` accepts an optional `s2s.base_url` (`wss://`) to target an OpenAI-Realtime-compatible gateway (Vercel AI Gateway, LiteLLM) or xAI direct — routes models like `xai/grok-voice-think-fast-1.0` over the same protocol. SSRF-gated by `BYOM_ALLOWED_URL_PATTERNS`. `s2s.model`/`s2s.voice` default to OpenAI's (`gpt-realtime-2.1`/`alloy`) whatever the provider, so each non-OpenAI path swaps those sentinels for its own — and passes an explicitly chosen value through untouched, so a wrong one is reported by the provider rather than silently replaced |

## Pipeline

Cascade (default):
```
transport.input → STT → [VoicemailDetector] → user_agg (VAD + SmartTurnV3) → [KnowledgeRetrieval] → LLM → TTS → [VM gate] → transport.output → asst_agg → observability
```

S2S (`pipeline_mode: "s2s"`):
```
transport.input → user_agg (VAD) → S2S_LLM (OpenAI Realtime / Gemini Live) → transport.output → asst_agg → observability
```

SMS/Chat (text-only):
```
inbound message → session lookup/create → message history → [KB retrieval]
  → [chat_tools: webhook + MCP] → LLM chat completion (tool loop) → store reply → respond
```

## SMS / Chat

Text-based conversations with agents via SMS or the Chat API.

### SMS Flow
```
Customer texts → Twilio POST /webhooks/twilio/sms/inbound → resolve phone → agent
  → create/resume session (24h TTL) → build LLM history → chat completion → TwiML reply
```

### Chat API
```
POST /v1/chat              # Send message, get LLM reply (creates/resumes session)
GET  /v1/chat/sessions     # List sessions
GET  /v1/chat/sessions/:id # Get session detail
GET  /v1/chat/sessions/:id/messages  # List messages
GET  /v1/chat/sessions/:id/tool-invocations  # Tool calls made in the session
DELETE /v1/chat/sessions/:id         # Expire session
```

Context threading: use `session_id` (group messages) or `previous_chat_id` (linear chain). Cannot use both.

### Session Management
- Auto-created on first inbound SMS or Chat API call
- Resumed if same (customer_number, turncall_number) pair and < 24h since last activity
- Expired after 24h inactivity (lazy on lookup + background cleanup every 15 min)
- Webhook events: `session.created`, `session.updated`, `session.deleted`, `chat.created`

### Phone Number Config
- `sms_enabled: true` on bind → auto-configures Twilio SMS webhook
- Same number handles both voice calls and SMS

## WhatsApp

Voice calls and text messages via WhatsApp Business Cloud API.

### WhatsApp Text Flow
```
Customer messages WhatsApp Business number → Meta POSTs /webhooks/whatsapp (field: "messages")
  → validate X-Hub-Signature-256 → resolve phone → agent
  → create/resume session (24h TTL) → build LLM history → chat completion
  → send reply via WhatsApp Cloud API (POST /{phone_number_id}/messages)
```

### WhatsApp Voice Flow
```
Customer calls WhatsApp Business number → Meta POSTs /webhooks/whatsapp (field: "calls")
  → validate X-Hub-Signature-256 → Pipecat WhatsAppClient handles WebRTC (SDP offer/answer)
  → pre-accept + accept call via Cloud API → Pipecat pipeline (16kHz SmallWebRTCTransport)
  → on "terminate" event: cleanup
```

### WhatsApp Webhook Setup
1. Set Callback URL: `https://<host>/webhooks/whatsapp`
2. Set Verify Token: must match `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
3. Subscribe to webhook fields: `messages` + `calls`

### WhatsApp Credentials (Meta Developer Console)
- `WHATSAPP_TOKEN`: WhatsApp > API Setup > Access Token
- `WHATSAPP_PHONE_NUMBER_ID`: WhatsApp > API Setup > Phone Number ID
- `WHATSAPP_APP_SECRET`: App Settings > Basic > App Secret (click Show, 32 hex chars)
- `WHATSAPP_WEBHOOK_VERIFY_TOKEN`: Any string you choose (shared with Meta webhook config)

### Phone Number Config
- `whatsapp_enabled: true` on phone number bind
- Same number can handle WhatsApp voice + text
- App must be in **live mode** for public access; development mode requires test numbers

## Video Avatar (HeyGen / Tavus)

Optional lip-synced video avatar on WebRTC calls. A Pipecat avatar `AIService`
consumes TTS audio and emits avatar video frames into the pipeline; the provider
runs its own WebRTC leg to its servers (HeyGen→LiveKit, Tavus→Daily), and
TurnCall's SmallWebRTC transport carries the video to the browser.

### Constraints
- **WebRTC + cascade only.** The avatar taps the `tts` stage, which S2S doesn't have. On a phone (Twilio/WhatsApp) or S2S agent it's skipped with a warning.
- **HeyGen** needs a **LiveAvatar key** (not a HeyGen key) — `HEYGEN_LIVE_AVATAR_API_KEY` from app.liveavatar.com. Pipecat targets `api.liveavatar.com`; HeyGen's old `/v1/streaming.*` API is sunset. Latency floor ~600ms+.
- **Tavus** needs `TAVUS_API_KEY` (platform.tavus.io) + a `replica_id`; `persona_id` defaults to `pipecat-stream` (lip-syncs Pipecat TTS). Sub-600ms, 1080p — higher quality/lower latency. Pulls `daily-python` (via the `tavus` extra) for its internal leg only — user transport stays SmallWebRTC.

### Config (per-agent)
```json
"transport": "webrtc", "pipeline_mode": "cascade",
// HeyGen:
"avatar": {"enabled": true, "provider": "heygen", "avatar_id": "<id>", "is_sandbox": true}
// Tavus:
"avatar": {"enabled": true, "provider": "tavus", "replica_id": "<id>"}
```

### Key Files
- `domain/models.py` — `AvatarConfig`; `api/v1/schemas/agents.py` — `AvatarConfigSchema` (required, or the field is dropped on API ingest)
- `orchestrator/pipeline_factory.py` — `_create_avatar_service()` builds HeyGen/Tavus, inserted between `tts` and `transport.output()`
- `orchestrator/transport_factory.py` — `video_out` enables live video on SmallWebRTC
- `api/v1/webrtc.py` — gates the avatar to webrtc+cascade
- See `adr/0002-heygen-avatar.md`

## Pre-Call Initialization (call-init)

Dynamic agent resolution before the pipeline starts. Works on all transports.

### Supported Transports
| Transport | Trigger | `call.type` value |
|-----------|---------|-------------------|
| Twilio voice | `routing_target_type: "webhook"` on phone number | `inboundPhoneCall` |
| WhatsApp voice | `routing_target_type: "webhook"` on phone number | `inboundWhatsAppCall` |
| WebRTC | `server_url` in POST /v1/webrtc/connect body | `webrtc` |

### Flow
```
Inbound call → resolve phone number → routing_target_type == "webhook"?
  → POST call-init to server_url
  → Parse response: agent_id | inline agent | variables | metadata | knowledge_context
  → Fire call.initializing event (informational)
  → Store metadata + knowledge_context + dynamic_config on call record
  → Apply template variables → prepend knowledge_context to system_prompt
  → Fire call.started event
  → Start pipeline
```

### Response Schema
```json
{
  "agent_id": "uuid",                     // OR "agent": {...inline config}
  "variables": {"name": "Jane", "tier": "premium"},
  "metadata": {"crm_id": "C-123"},
  "dynamic_data": {"knowledge_context": "Customer has open ticket #456..."}
}
```

### An inline agent has no agent row

`agent` (inline config) instead of `agent_id` means there is nothing in `agents`
to point at, so `calls.active_agent_id` stays **null** for the whole call and
every event carries `agent_id: null`. Two rules hold it together, and every voice
transport obeys both (`adr/0017`):

- `CallContext(agent_id=... or DYNAMIC_AGENT_ID)` — the zero-UUID sentinel from
  `orchestrator/pipeline_factory.py`, never a locally invented one, and never
  written to `active_agent_id`.
- the config is stored raw as `metadata_json["dynamic_config"]` by whatever
  creates the call row. `services/call_analysis_trigger.config_for_call()` is
  the only reader — post-call code must go through it rather than
  `agent.config_blob`, **log lines included**.

Dropping either is silent: the first ends the call before the pipeline starts,
the second skips post-call processing, and since that trigger dispatches
`call.ended`, it skips the webhook too. `tests/unit/test_inline_agent_context.py`
guards both structurally.

### Key Files
- `services/call_init_resolver.py` — Shared response parser (frozen dataclass result)
- `services/template_renderer.py` — `render_agent_config()` + `prepend_knowledge_context()`
- `events/server_events.py` — `send_call_init()` with `transport_type` param

## Knowledge Base (RAG)

Upload documents and attach them to agents for retrieval-augmented generation. Three retrieval modes:

### Retrieval Modes

| Mode | Behavior | Best For |
|------|----------|----------|
| `prompt` | Full document text injected into system prompt at call/chat start | Small docs (<5KB): FAQs, company info |
| `auto` | Per-turn semantic search via pgvector, context injected before LLM | Product catalogs, policies (always-relevant) |
| `tool` | LLM calls `query_knowledge` tool when it decides to search | Large tech docs, archives (selective retrieval) |

### Architecture

```
Document Upload → Extract + Clean (PDF/TXT/DOCX) → Chunk (token-based) → Contextual Enrichment (LLM, best-effort) → Embed (OpenAI) → Store (pgvector + tsvector)

Retrieval: hybrid — vector KNN + Postgres full-text, RRF rank fusion (ADR-0012).
Auto-mode query = previous user turn + last agent reply + current utterance.
Eval: scripts/eval_retrieval.py + scripts/rag_golden.yaml (hit@k, MRR).

Voice call: transport.input → STT → user_agg → [KnowledgeRetrievalProcessor] → LLM → TTS → transport.output
Chat/SMS:   inbound message → session → [KB retrieval] → LLM completion → reply
```

### API

```
POST   /v1/knowledge-bases                              # Create KB
GET    /v1/knowledge-bases                              # List KBs
GET    /v1/knowledge-bases/{kb_id}                      # Get KB
PUT    /v1/knowledge-bases/{kb_id}                      # Update KB
DELETE /v1/knowledge-bases/{kb_id}                      # Delete KB (blocked if agents depend on it)

POST   /v1/knowledge-bases/{kb_id}/documents            # Upload document (multipart)
GET    /v1/knowledge-bases/{kb_id}/documents            # List documents
GET    /v1/knowledge-bases/{kb_id}/documents/{doc_id}   # Get document
DELETE /v1/knowledge-bases/{kb_id}/documents/{doc_id}   # Delete document + chunks

POST   /v1/knowledge-bases/{kb_id}/search               # Test search (debug)

POST   /v1/agents/{agent_id}/knowledge-bases            # Link KB to agent
GET    /v1/agents/{agent_id}/knowledge-bases            # List agent's KBs
DELETE /v1/agents/{agent_id}/knowledge-bases/{kb_id}    # Unlink KB from agent
```

### Supported File Types

PDF, TXT, Markdown, DOCX, CSV, JSON, YAML, XML, TSV

### Key Files

- `services/document_ingestion.py` — Upload, extract, chunk, embed pipeline
- `services/retrieval.py` — Query embedding + pgvector search + formatting
- `orchestrator/knowledge_processor.py` — Pipecat FrameProcessor (auto mode) + tool handler (tool mode)
- `storage/repositories/knowledge_repo.py` — KB, document, chunk, link CRUD
- `api/v1/knowledge.py` — REST endpoints
- `adapters/storage/` — Local filesystem and S3 storage adapters

### Configuration

Embedding model configurable per knowledge base (default: `text-embedding-3-small`, 1536 dims).
Chunk size and overlap configurable per KB. Agent attachment specifies mode, top_k, similarity_threshold.

Requires PostgreSQL with `pgvector` extension (`CREATE EXTENSION IF NOT EXISTS vector`).

## Agent Versioning

Linear version model with auto-promotion. Each published version is immutable.

### Lifecycle

```
POST /agents → v1 (draft) → PUT /agents/v1 (edit) → POST /agents/v1/publish → v1 live
POST /agents → v2 (draft) → POST /agents/v2/publish → v2 live, v1 auto-archived, phone numbers auto-promoted
POST /agents/v1/rollback → v1 restored, v2 archived, phone numbers updated
```

States: `draft` → `published` → `archived`

### Key Files
- `api/v1/agents.py` — Publish with auto-archive + auto-promote, versions, rollback
- `storage/repositories/agent_repo.py` — `archive_previous_published()`, `update_phone_number_routing()`, `list_versions()`

## A/B Testing

Weighted traffic routing on phone numbers. Deterministic by caller number (SHA256 hash).

### API

```
PUT    /v1/phone-numbers/{id}            # Update binding in place (id + server_url_secret stable)
PUT    /v1/phone-numbers/{id}/routing    # Set weights (must sum to 100)
GET    /v1/phone-numbers/{id}/routing    # Get routing config
DELETE /v1/phone-numbers/{id}/routing    # Clear test, revert to single agent
```

### Key Files
- `services/weighted_routing.py` — `pick_agent_by_weight()` (SHA256 deterministic selection)
- `webhooks/twilio_handlers.py` — Inbound routing checks `routing_weights`
- `api/v1/phone_numbers.py` — Routing CRUD endpoints

## Takeaways (Structured Outputs)

Reusable post-call extractions (ADR-0013). Define once (`name`, JSON `schema`, optional `prompt`/`model`), attach via `analysis.takeaway_ids`, results keyed by name in `call.ended` → `analysis.takeaways` (`{result, valid, model, duration_ms}`). One concurrent LLM call per takeaway, schema-validated with one retry.

```
POST/GET  /v1/takeaways        # CRUD (name immutable, schema validated at create)
GET/PUT/DELETE /v1/takeaways/{id}   # delete blocked (409) while attached to agents
```

Key files: `api/v1/takeaways.py`, `storage/repositories/takeaway_repo.py`, `services/call_analysis.py` (`extract_takeaway`), `services/call_analysis_trigger.py` (`_extract_takeaways`).

## Evals

Automated behavioural testing for agents (#68, ADR-0018). A **scenario** is one
saved test — **scripted** (`turns:`, fixed conversation with per-turn
expectations) or a **simulation** (`persona:`, an LLM plays the caller). A
**run** is one scenario x target x modality over N iterations.

The engine is `pipecat.evals`, already pinned via pipecat 1.11. **Only the
transport is swapped** — pipecat's harness is an RTVI WebSocket client and the
bot hosts `EvalTransport`, so an eval exercises the real STT/LLM/TTS
construction, the real VAD and smart-turn wiring, the real tool bridge and KB
retrieval. That construction path is where #63, #64, #65 and #67 all lived.

### API
```
POST/GET/PUT/DELETE /v1/eval-scenarios[/{id}]   # ?kind= ?tag= ?page= ?limit=
POST   /v1/eval-runs          # 202 Accepted -- the worker executes it
GET    /v1/eval-runs          # ?batch_id= ?scenario_id= ?status= ?page= ?limit=
GET    /v1/eval-runs/batches/{batch_id}   # one verdict, no transcripts (#75)
GET    /v1/eval-runs/{id}     # ?view=summary for the verdict without the payload
DELETE /v1/eval-runs/{id}     # cancel while queued/running
```

Both lists page (`page`/`limit`, newest first, `id` breaking ties so a page
cannot repeat or skip a row) and answer in the `paginated` envelope with
`total`. The **tag fan-out** is the deliberate exception: `POST /v1/eval-runs`
reads every scenario carrying the tag, because a suite that silently ran one
page of itself reports a verdict for scenarios that never ran (#97 caps that
instead). `?view=summary` is the same trade the batch endpoint makes — a full
run read carries every iteration's transcript and all three snapshots, which is
a lot to poll on a timer.

`POST /v1/eval-runs` takes **exactly one** of `scenario_id` and `tag`. A `tag`
fans out to one run per scenario carrying it, sharing a `batch_id`, all returned
in the 202 — and a tag matching nothing is a 400, because an empty batch reports
"0 failures" forever, which reads as a pass. Grouping is deliberately a tag
array plus a batch id: no suite table, no join table. The batch verdict is the
run verdict one level out — anything in flight is `running`, any failed run
fails it, nothing reaching a verdict is `errored`, and an errored run counts
toward neither rate. Its counts are **runs**, not iterations.

### Simulations (#73)

A `persona:` definition runs too now, through its own result mapper — handing a
simulation result to the scripted one would report nonsense confidently. The
judge rules on `success` once over the whole conversation; `metrics` are scored
per bot turn (a share of turns, so 0.8 is four replies in five) and one below
its `min_score` fails the iteration, while one without a `min_score` reports and
fails nothing. The entry carries `goal`, `metrics`, `ended_by`, `persona_turns`
and `persona_claim` — the persona's own end-of-call verdict, stored with
`advisory: true` on it, because pipecat is explicit that the **judge** decides
and showing the two as equals teaches people to distrust the judge.

A persona doesn't say the same thing twice, so one run is an anecdote: use
`iterations`. There is still no score column — a scripted `1/1` and a simulation
`7/10` are one representation read twice. **The persona and the judge are both
Ollama by default**, so a simulation needs a reachable Ollama even when the
agent's own LLM is a hosted provider.

### Targets (#74)

```jsonc
{"type": "agent",      "agent_id": "uuid"}   // an agent row IS a version — pins it
{"type": "agent_name", "name": "support"}    // whatever is published at run time
{"type": "inline",     "agent": {...}}       // no row at all; the tool sandbox
```

Pinning by id stops testing production the moment the next version is
published — that is what `agent_name` is for. A name with nothing published
**fails the run**; it never falls back to a draft. The run records
`agent_version` next to `agent_id` because that column has no foreign key and
the row can be deleted. An inline config is validated at the boundary by the
same `AgentConfigSchema` an agent create uses (`extra="forbid"` included), runs
with **`agent_id` null** (ADR-0017 — null is the honest answer, not missing
data), and is snapshotted verbatim in `resolved_config`.

### Scenario from a real conversation (#78, #87)

```
POST /v1/eval-scenarios/from-call      {"call_id": "...",    "save": false, "name": "..."}
POST /v1/eval-scenarios/from-session   {"session_id": "...", "save": false, "name": "..."}
```

Evals only find what someone thought to test; the unknown unknowns come from
production. A call already holds a scripted scenario's shape — caller turns,
agent replies, tool calls with their arguments — so this converts one: caller
utterances become turns, replies become `text_contains` expectations **to
sharpen**, and every recorded tool invocation becomes both a `function_call`
expectation carrying the arguments actually used *and* a **mock seeded with
what that tool really returned**, so the draft is safe under `mock_only`
without repeating the call's side effects.

`save: false` (the default) returns the draft for review; `true` stores it. The
response says out loud that it is a draft — a generated scenario nobody edits
asserts whatever the agent did that day, mistakes included.

Both routes share one converter: `build_scenario` takes a normalised
`Utterance`, and each source brings its own reader
(`utterances_from_call_events` for `{role, text}` event payloads,
`utterances_from_session_messages` for the `role`/`content` columns) — so it is
source-agnostic by construction rather than by claim. A `system` message is
dropped: neither party said it, and a scripted scenario has nowhere to put it.
Consecutive caller utterances are merged, because one sentence the STT split in
two is one turn, not two. A session names its agent directly so its
`default_target` needs none of the inline-config reading a call record requires.
`POST /v1/webrtc/connect` returns `call_id` beside `sdp`/`type` (additive) so a
browser that just held a WebRTC conversation can convert it.

Two shapes that are easy to get wrong. A `function_call` expectation takes a
**`calls:` list** (`[{name, args}]`), not top-level `name`/`args`: the parser
ignores keys it does not know, so the wrong shape parses cleanly and asserts
nothing. And the reply expectation must carry **content**, never a bare
`{"event": "llm_response"}`, for the reason evals-design §1 records. A call
that ran an inline agent takes its `default_target` from the call record, not
an agent row (ADR-0017); one still in progress, or with no caller speech, is
rejected.

### CLI (#77)

```bash
export TURNCALL_API_URL=https://api.example.com TURNCALL_API_KEY=tc_...
turncall eval run --tag pre-publish --agent-name support   # a whole set
turncall eval run scenarios/*.json --agent-id <uuid>       # local files
turncall eval list --batch <batch_id>
turncall eval show <run_id>                                # transcript + verdicts
```

**Exit 0 only when every run passed.** A failure is `1`, an error `2`, a
cancellation `3`, a batch still running when the command stopped waiting `4`, a
usage mistake `64` — distinct because "your agent regressed" and "we could not
check" want different alerts, and a judge outage must never read as a
regression. `4` is separate from `2` for the same reason one step on (#112): an
errored run is terminal and means nobody could tell, while a timed-out one is
still being executed and will reach a real verdict minutes later — and the
remedy is `--timeout` or more worker slots, not the judge. Precedence is by how
loudly it should be read: a failure outranks an error, which outranks a run
that simply has not finished. An empty batch exits non-zero: reporting success
for a batch that ran nothing is how a green pipeline stops meaning anything.

A scenario file is **the API request body, unchanged** — no CLI-only fields and
no YAML dialect, so there is one schema and one validator. `POST /v1/eval-runs`
therefore takes a third selector beside `scenario_id` and `tag`: an inline
`scenario`, run without being stored (`scenario_id` null on the run, the
snapshot is the record). Target comes from `--agent-id` / `--agent-name` /
`--inline-agent`, falling back to the scenario's `default_target`; for a tag,
every matching scenario has to agree on that default, since one request carries
one target.

```yaml
# .github/workflows/agent-evals.yml
- run: pip install turncall
- run: turncall eval run --tag pre-publish --agent-name support
  env:
    TURNCALL_API_URL: ${{ vars.TURNCALL_API_URL }}
    TURNCALL_API_KEY: ${{ secrets.TURNCALL_API_KEY }}
```

### Rules that are easy to break
- **The worker is never the API process.** `turncall-eval-worker`, same image,
  own entrypoint, fed by a Redis list. ADR-0004: eval load in the API's event
  loop becomes dead air on a live call.
- **`definition` is pipecat's mapping, stored verbatim**, validated by
  round-tripping through pipecat's parser; `schema_version` records which
  pipecat schema it targets. `tool_mocks`/`tool_policy` are TurnCall columns
  *outside* it. That round-trip uses two of pipecat's **private** functions
  (`_parse_script`/`_parse_simulation`) because 1.11's public loaders take a
  *path* and a stored scenario has no file — so a pipecat bump that moves them
  takes out every create, update and queued run at once.
  `tests/unit/test_pipecat_parser_contract.py` is what makes that a red build
  naming the version instead (#100).
- **`errored` is not `failed`.** The harness not completing is neither a pass
  nor a fail and never counts toward a rate. No `score` column —
  `passed_count`/`failed_count` out of `iterations`.
- **Three snapshots per run**: `resolved_config`, `resolved_scenario`,
  `harness_config`. ADR-0017's rule one level out.
- **An eval has no `calls` row.** `CallContext.eval_run_id` / `.is_eval` gates
  every call-scoped side effect — status writes, call_events, transcript taps,
  `tool_invocations` (there is no row to hang them off), and `call.ended`
  (which is also what triggers post-call analysis).
- **Tools are mocked, and fail closed by default (#71).** The scenario's
  `tool_mocks` reach `CallContext.tool_mocks` and `services/tool_mocks.intercept`
  short-circuits the dispatch *before* the branch that would run it — webhook,
  MCP and built-in alike. Under `mock_only` (the default) a tool with no mock is
  refused and the iteration is `errored` naming it; `live` is the typed opt-in
  for read-only tools. What each iteration called lands in its results entry as
  `tool_calls`, each marked `mocked`. Mocks are the **scenario's**, never the
  run's: "the booking succeeds" and "the booking fails" are two tests.

### Writing a scenario that actually catches a provider regression
Assert **content**, not just the event. After a provider 404 pipecat still
emits an empty `llm_response`, so `{"event": "llm_response"}` alone can pass
with the LLM completely broken — seen both ways on one config. Use
`text_contains`/`text_excludes`/`eval:` — **not** `matches:`, which pipecat 1.11
has no field for and its parser silently drops, so an expectation resting on it
asserts nothing at all. Validation is a round-trip through pipecat's
parser and checks *shape*, not strength, so this one is checked separately and
**warned about, never rejected** (#95): the create/update response and the run
both carry `scenario_cannot_fail` when no expectation asserts content, and
`content_free_expectations` naming the weak ones when only some do — and a
`matches:`-only expectation is one of the things it catches. A
`function_call` with its `calls:` is a real assertion; a simulation is exempt,
since its judge rules on `success` over the whole conversation. Such a run scores **`failed`**, never
`errored`; errored means the harness could not complete and is kept out of
every rate, which would hide exactly the #63/#64/#65 class evals exist for.

### Audio modality (#72)

`modality: audio` on the run: the caller's turns are synthesized and reach the
agent's **real STT**, the agent answers through its **real TTS**, and the judge
reads a transcription of the audio that was actually produced — the `response`
event, which is what a scenario should assert on in audio mode (`llm_response`
is the model's text and skips both ends). Text stays the default: it is what
people run per PR, and it is a fraction of the time.

Two services the text path never builds, both **local and downloaded on first
use** (`~/.cache/pipecat`): Kokoro speaks the caller, Moonshine transcribes the
agent for the judge. Pipecat *requires* both to be named as soon as the
modality is audio — it raises otherwise — so `with_modality` fills the pair a
scenario did not name (`DEFAULT_USER_SPEECH` / `DEFAULT_BOT_TRANSCRIPTION`),
and a scenario's own `user.speech:` / `judge.transcription:` always wins.

A run's results entry carries **both** views of each reply: `content` is what
the judge read, `text` the agent's own words when the two differ. That
difference is the whole explanation when an audio run fails where a text run
passed. `harness_config` records which voice and which STT produced them,
for the same reason it records the judge model.

**S2S agents work in both modalities** — measured against Gemini Live, not
assumed: the S2S service emits its own LLM text, so a text-mode judge has
`llm_response` to read. Nothing enforces audio-only, because nothing needs to.
Nova Sonic is untested.

### Known coverage limits
Everything *inside* the transport is invisible: the Twilio serializer and the
whole ADR-0004 audio class, output underrun and dead air (loopback does not
pace in realtime, so evals measure latency, not silence), and telephony.
**An eval connects no MCP servers.** It builds through `build_call_pipeline`,
which takes no MCP manager, so an agent's MCP tools are neither contacted (the
point of #71) nor advertised — the model never sees them, and a `tool_mocks`
entry naming one never fires. The run warns rather than failing — in the run's
own **`warnings`** list (`{code, message, tools, mcp_servers}`), which rides to
`eval.run.completed`, the batch summary and `turncall eval show`, because the
author reads the run and never the worker's log. `mcp_servers` is the valuable
half: it says "this is the MCP limitation, not your typo". `live_tools_allowed`
is the other one — a `live` policy really executes the agent's tools, once per
iteration. **Text mode also cannot see the agent's `first_message`** — it goes out as a
`TTSSpeakFrame`, so it never becomes LLM text, and `skip_tts` silences the TTS.
The judge is pipecat's `EvalJudge`, **Ollama by default** — but a scenario can
name another (#118): `judge: {provider, model, temperature, endpoint}` and
`simulator: {...}` are TurnCall columns beside `tool_mocks`, compiled into
pipecat's blocks at parse time. `provider` is a closed set (`ollama`, `openai`,
`anthropic`) mapping to factories **TurnCall ships**, because pipecat's own
escape hatch is `factory` — a dotted path it hands to `importlib.import_module`,
which from a request body is remote code execution. A definition naming one
anywhere pipecat reads it (`judge.eval`, `judge.transcription`, `simulator`,
`user.speech`) is refused at the API boundary. Temperature has no pipecat field
and rides in `extra`; it defaults to unset, and is dropped for models that
reject it — Anthropic and the OpenAI reasoning families — by the same rule the
call path follows. The **key is the platform's**, read from `ANTHROPIC_API_KEY`
/ `OPENAI_API_KEY` exactly as the agent's own LLM reads it: a credential a
scenario could set would be stored in JSONB, returned by a read and masked
forever after (#91), so `api_key` in a judge block is a 422. `endpoint` is the
one field that travels — an OpenAI-compatible gateway, or Ollama's URL. A raw `judge:` block inside `definition` still wins, so a
stored scenario's verdicts keep being decided by the model it named. Without any
of that, an `eval:` assertion needs a reachable Ollama and
errors without one, while `text_contains`/`function_call` build no judge at
all.

A judge can also be set **once**, for the platform: `EVAL_JUDGE_PROVIDER` /
`_MODEL` / `_TEMPERATURE` and the `EVAL_SIMULATOR_*` trio (#119). Precedence is
narrowest-wins — the scenario's block, then those, then pipecat's Ollama — and
a block is taken **whole**, never merged field by field, since a platform
`model` landing on a scenario's provider names a model that provider has never
heard of. There is **no run-level override**: a `judge` on `POST /v1/eval-runs`
is a 422, because a run is what it was queued as and two runs of one scenario
decided by different judges are incomparable with nothing on either row saying
why — the same reason mocks belong to the scenario. An inline scenario (#77)
carries its own, like the stored one it mirrors. On `PUT
/v1/eval-scenarios/{id}` those two fields are the only ones where a sent
**null** means something — "back to the platform default" — so they are applied
whenever they were sent, while every other field still only applies when it is
not null. Without that a scenario's judge was permanent the moment it had one.

Which judge answered is then recorded and **acted on**. `harness_config` carries
`judge_provider` and `judge_temperature` beside `judge_service`/`judge_model`/
`judge_factory` (the first two are what the dotted path and `extra` swallowed),
and on `start_run` the resolved judge is compared with the last run of the same
scenario that reached a verdict — one query on `ix_eval_runs_scenario`. A
difference adds a `judge_changed` warning naming both, because a red run that
nobody can attribute to a judge swap is the expensive version of this. Only keys
the older snapshot actually recorded are compared: rows written before #119 have
no `judge_provider`, and reading that absence as a change would announce one on
every scenario's next run.

`harness_config` records **which code ran it** for the same reason:
`worker_version`, `worker_started_at`, and `worker_stale` — true when the
package's newest source file is younger than the process that imported it. A
packaged image bakes its source at build, so this stays false there; a
bind-mounted dev worker left running while the code moved under it reports
true, and the run carries a `worker_stale` warning saying when each happened.
That case is silent by construction — the run succeeds and the verdict looks
ordinary, while the feature the scenario depends on was simply not in the
process — and it cost a day and a database query to diagnose once already.

### Config
`EVAL_MAX_CONCURRENT_RUNS` (4), `EVAL_MAX_RUN_DURATION_SECONDS` (900). That
number is a **run** budget, split across the run's iterations to bound each one:
one iteration is abandoned as `errored` after `max(900/iterations, 180s)`,
floored so a 50-iteration run is not handed 18s per conversation. Unbudgeted, a
provider that accepts a connection and never answers held a worker slot for the
life of the process (#93). The janitor's cutoff for a claimed run is then that
arithmetic back out — `max(900, 180 × iterations) + 60s`, **per row** — because
there is no heartbeat, and a flat 900s swept healthy multi-iteration runs
mid-flight, which the CLI reported as a regression (#94). `EVAL_MAX_QUEUED_SECONDS`
(3600, the cutoff for a run never claimed), `EVAL_JANITOR_INTERVAL_SECONDS` (60),
`EVAL_MAX_ITERATIONS` (50), `EVAL_TTS_CACHE_DIR`. `EVAL_JUDGE_PROVIDER` /
`EVAL_JUDGE_MODEL` / `EVAL_JUDGE_TEMPERATURE` and `EVAL_SIMULATOR_*` are the
platform's judge and persona, all unset by default (#119).

### Key Files
- `evals/harness.py` — the bridge: real pipeline one end, pipecat's session the other
- `evals/runner.py` — target resolution, the iteration loop, status derivation, result mapping
- `evals/worker.py` — `turncall-eval-worker`: queue consumer, concurrency cap, janitor
- `evals/scenario.py` — parse/validate a stored definition; modality merge
- `orchestrator/transport_factory.py` — `create_eval_transport()`
- `api/v1/evals.py`, `storage/repositories/eval_repo.py`
- See `adr/0018-eval-transport-bridge-and-worker.md`

## Post-Call Analysis

Automatic LLM-powered analysis after a call ends. Results ship inside `call.ended`.

### Flow
```
Call ends → background processing (analysis ~2-5s + recording flush) → single call.ended webhook (analysis inline)
```

(`analysis.completed` exists in the enum but is never dispatched — the analysis is part of the `call.ended` payload.)

### Analysis Config (in agent config)
```json
{
  "analysis": {
    "enabled": true,
    "summary_enabled": true,
    "summary_prompt": "Custom summary instructions...",
    "success_evaluation": {"enabled": true, "rubric": "...", "scale": "pass_fail"},
    "sentiment_enabled": true,
    "structured_extraction_schema": {"type": "object", "properties": {...}},
    "scoring_rubric": {"criterion_name": {"max_score": 10, "description": "..."}},
    "model": "gpt-4o"
  }
}
```

### API
```
GET  /v1/calls/{id}/analysis         # Get analysis results
POST /v1/calls/{id}/analysis/rerun   # Re-run analysis
```

### Key Files
- `services/call_analysis.py` — LLM-based analysis (prompt building, parsing, AnalysisResult)
- `services/call_analysis_trigger.py` — Background task trigger + event dispatch
- `api/v1/calls.py` — Analysis GET/rerun endpoints
- `api/v1/schemas/agents.py` — `AnalysisSchema`, `SuccessEvaluationSchema`

## Tools

### Built-in Tools
| Name | Description | Parameters |
|------|-------------|------------|
| `end_call` | Terminate the call | `reason` (optional) |
| `transfer_call` | Transfer to phone number (cold/warm) | `target_number` (required), `transfer_mode`, `transfer_message`, `briefing`, `fallback_message`, `reason` |
| `handoff_to_agent` | Switch to another agent | `agent_id` (required), `reason`, `context` |
| `send_dtmf` | Send keypad tones | `digits` (required) |

### Custom Webhook Tools
Require `webhook_url`. TurnCall POSTs:
`{tool_name, arguments, project_id, call_id, session_id}` — exactly one of
`call_id` (voice) / `session_id` (SMS, chat, WhatsApp text) is set, the other null.
Optional `webhook_secret`: when set, each POST is HMAC-signed (`X-TurnCall-Signature: v1=<hex>`,
`X-TurnCall-Timestamp`, HMAC-SHA256 over `"{timestamp}.{body}"` — same scheme as event webhooks).

### Tool Schema
```json
{
  "name": "snake_case_name",
  "description": "When/why the LLM should invoke this",
  "parameters_schema": {"type": "object", "properties": {...}, "required": [...]},
  "execution_mode": "sync",          // "async" survives an interruption (voice only)
  "webhook_url": "https://...",
  "webhook_secret": "optional — HMAC-sign tool POSTs",
  "timeout_seconds": 10,
  "max_retries": 1
}
```

`execution_mode` (voice only): `sync` (default) cancels an in-flight call when the caller talks over the agent; `async` lets it finish and delivers the result when it arrives — for a lookup slower than the conversation. Maps to Pipecat's `cancel_on_interruption`. Text turns are request/response, so the field is ignored there.

### MCP Tools (Model Context Protocol)
Connect agents to MCP servers for auto-discovered tools. Tools are fetched at call start via `tools/list` and registered alongside webhook/builtin tools.

```json
{
  "mcp_servers": [
    {"name": "crm", "transport": "http", "url": "https://mcp.example.com/mcp"},
    {"name": "local-db", "transport": "stdio", "command": "python", "args": ["server.py"]}
  ]
}
```

| Transport | Config | Notes |
|-----------|--------|-------|
| `http` | `url` + optional `headers` | Streamable HTTP (recommended) |
| `sse` | `url` + optional `headers` | Server-Sent Events |
| `stdio` | `command` + `args` + `env` | Local subprocess (requires `MCP_STDIO_ENABLED=true`) |

### Key Files
- `services/mcp_client.py` — `MCPSessionManager`: connect, discover, call, cleanup. Works against **both** MCP SDK lines (`mcp>=1.27,<3`): 2.x renamed `Tool.inputSchema`→`input_schema` and `CallToolResult.isError`→`is_error`, so the client reads whichever spelling is present, and uses `streamable_http_client` — the transport name 1.24+ and 2.x share — building its own HTTP client for headers/timeout from whichever httpx family the SDK was built on
- `orchestrator/tool_bridge.py` — Routes MCP tool calls through MCP client
- `orchestrator/pipeline_factory.py` — Merges MCP tools into pipeline at creation
- `orchestrator/pipeline_builder.py` — `start_call_pipeline()`: MCP discovery for WebRTC + WhatsApp voice (in the task that also runs the call — MCP transports open anyio cancel scopes that must be exited where they were entered)
- `webhooks/media_stream.py` — MCP discovery before pipeline start (Twilio)
- `services/chat_tools.py` — webhook + MCP tools for text turns (per-message connect/close)
- `services/tool_webhook.py` — the shared webhook POST + HMAC signing, used by both paths
- `services/url_allowlist.py` — `check_url_allowed()`: MCP urls + BYOM/S2S base_urls share one SSRF gate (`BYOM_ALLOWED_URL_PATTERNS`; empty = allow all). Matches the whole URL **and** the host separately, so the trusted name can't be smuggled into a path, query or `user@` prefix

Tool names are flat and unique: precedence is built-in > agent `tools` > MCP (server order — claimed serially after a concurrent discovery round, so the agent's config decides, not which server answered first); a collision is skipped + logged. That order is enforced twice — in what gets advertised (`_build_tools_schema`) *and* in what actually runs (`tool_bridge`, `chat_tools`), which have to agree. `handoff_to_agent` swaps the prompt and the target's `tools` (via `LLMSetToolsFrame`) but does **not** re-connect MCP servers mid-call.

Tools run on voice **and** text, on every LLM provider — OpenAI-compatible (`tools`/`tool_calls`), Anthropic (`input_schema` + tool_use/tool_result blocks) and Bedrock Converse (`toolSpec`/`toolUse`/`toolResult`) each have their own dialect in `llm_text.py`. Built-ins are voice-only — all four resolve through `call_control` against a live `call_id`. Calls within one round run concurrently. Text turns cap at `_MAX_TOOL_ROUNDS` (5), then re-ask with the tools withheld so a reply always goes out.

### Tool Invocation Recording
All tool calls (webhook + MCP + builtin) are recorded in `tool_invocations` with input, output, status, latency_ms, and dispatched as `tool.result` events.

A row belongs to a **voice call** (`call_id`) or a **text session** (`session_id`) — a CHECK enforces exactly one. Read them back with `GET /v1/tools/invocations/{call_id}` or `GET /v1/chat/sessions/{session_id}/tool-invocations`.

Recording runs off the reply's critical path and is best-effort: a failure is logged, never raised, so losing the audit row can't cost the caller their answer.

## Pipecat Integration

All Pipecat imports isolated in `orchestrator/`. No other module imports Pipecat.

- `serializer.py` — Twilio mulaw ↔ PCM16 frames
- `pipeline_factory.py` — Builds pipeline from AgentConfig (providers, VAD, Smart Turn, voicemail)
- `transport_factory.py` — Creates Twilio, WebRTC, or WhatsApp transport
- `call_session.py` — Per-call lifecycle, first message, cleanup
- `tool_bridge.py` — Registers tools + writes ToolInvocation records
- `observability.py` — Logs transcripts/events to DB + dispatches webhooks
- `telemetry.py` — OpenTelemetry tracing setup + Pipecat observers (latency/turn/LLM/transcription/startup). See ADR-0010
- `session_manager.py` — Active session registry

## Webhook Events

Subscribers (`POST /v1/webhooks`) receive a signed envelope per event:

```json
{
  "event": "call.ended",
  "project_id": "uuid",
  "call_id": "uuid | null",
  "session_id": "uuid | null",   // set on sms/chat events
  "eval_run_id": "uuid | null",  // set on eval.run.* events (#76)
  "agent_id": "uuid | null",     // the call's active agent (handoff-aware); null for an inline agent
  "event_id": "uuid",            // unique; stable across retries → dedupe key
  "timestamp": "ISO-8601",
  "payload": { /* event-specific */ }
}
```

Headers: `X-TurnCall-Signature` (HMAC-SHA256), `X-TurnCall-Timestamp`, `X-TurnCall-Event`.

`call.ended` payload: `status`, `ended_reason`, `from_number`, `to_number`, `direction`,
`duration_ms`, `provider_call_sid`, `metadata`, `started_at`, `ended_at`,
`recording_status`, `recording_url`, `summary`, `analysis`, and
`transcript` (`[{role, text, timestamp}]`).

`ended_reason` (derived, not stored): `customer_ended_call`, `assistant_ended_call`,
`customer_did_not_answer`, `customer_busy`, `customer_silent`, `voicemail`, `transferred`,
`max_duration_reached`, `pipeline_error`, `telephony_failed`, `unknown`.

`customer_silent` comes from `user_idle_timeout_ms` (default `10000`, `0`
disables): after the agent stops speaking, a caller who stays quiet that long
hears `idle_message` ("Are you still there?"), and a second consecutive silence
ends the call. Speaking again resets the count. Cascade speaks the line through
TTS; S2S has no TTS stage, so the model is asked to check in and words it
itself. Like `max_duration_reached` it records a marker event
(`call.customer_silent`) and is inferred **above** `assistant_ended` — ending
the call is how the guard gives up, so without its own branch it would read as
`assistant_ended_call`. Not to be confused with `silence_timeout_ms`, the VAD
stop window inside a turn; see CONTEXT.md, "the three timeouts".

`max_duration_reached` comes from `max_call_duration_seconds`: a watchdog on the
`CallSession` records `call.max_duration_reached` and cancels the worker, so the
call finalizes the same way a hangup does. Without its own reason it read back
as `customer_ended_call`. `interruption_enabled: false` turns off barge-in via
the user turn-start strategies — **cascade only**; on S2S the realtime service
owns turn-taking and the setting is warned about rather than half-applied.

`eval.run.started` / `eval.run.completed` are the eval pair (#76). The completed
one is **comprehensive** — status, `passed_count`/`failed_count` out of
`iterations`, `error`, every iteration's entry (transcript, failures,
`tool_calls`, and a simulation's goal + metrics), and all three snapshots
(`resolved_config`, `resolved_scenario`, `harness_config`) — so a subscriber
never has to call back. Same precedent as `call.ended`, and the reason
`analysis.completed` is the counter-example nobody should copy. An **errored**
run dispatches it too: accepted-then-silent would leave a subscriber waiting on
a terminal event forever. The run id is in the **envelope**, never the payload;
`agent_id` resolves as always and is null for an inline target.

Key files: `events/webhook_delivery.py` (envelope + signing + retry),
`events/dispatcher.py` (agent_id/event_id resolution), `domain/call_state.py`
(`infer_ended_reason`). See `adr/0007`, `adr/0008`.

## API Convention

- Response: `{"success": true, "data": ...}`
- Auth: `Authorization: Bearer tc_...`
- Errors: `{"success": false, "error": "...", "code": "..."}`
- Project-scoped: all queries filtered by API key's project
- TwiML: single `callId` param + `statusCallback`

## Agent skills

### Issue tracker

GitHub Issues in `kobikis/turncall`, via the `gh` CLI.

### Triage labels

The five canonical roles under their default names: `needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`.

### Domain docs

Single-context: `CONTEXT.md` and `adr/` at the repo root — not `docs/adr/`, which
is where the sibling builder repo puts its ADRs.
