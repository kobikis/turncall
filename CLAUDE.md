# TurnCall

Production voice agent platform. API-only backend for real-time AI voice agents over Twilio PSTN, WebRTC browser calls, WhatsApp Business (voice + text), and SMS/chat text conversations.

## Stack

- **Runtime**: Python 3.12, FastAPI, Pipecat 1.8
- **Database**: PostgreSQL (asyncpg + SQLAlchemy async + Alembic)
- **Cache**: Redis
- **Telephony**: Twilio Voice + Media Streams (WebSocket)
- **WhatsApp**: WhatsApp Cloud API + Pipecat WhatsAppClient (voice calls + text messages)
- **SMS/Chat**: Twilio SMS + Chat API (text conversations with session management)
- **WebRTC**: Pipecat SmallWebRTCTransport (browser calls)
- **Video Avatar**: HeyGen LiveAvatar via Pipecat HeyGenVideoService (WebRTC + cascade only)
- **STT**: Deepgram Nova-2 (streaming) · OpenAI Whisper · ElevenLabs Scribe · Cartesia Ink
- **LLM**: OpenAI GPT-4o-mini · Anthropic Claude (Sonnet, Haiku, Opus) · Ollama (local models) · OpenRouter (multi-model + fallback routing) · Any OpenAI-compatible endpoint
- **S2S**: OpenAI Realtime · Gemini Live (native audio-in/audio-out)
- **TTS**: Deepgram Aura-2 · OpenAI TTS-1 · ElevenLabs Flash v2.5 · Cartesia Sonic-3.5
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
make test             # pytest with coverage
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
| `BYOM_ALLOWED_URL_PATTERNS` | No | JSON list of allowed base_url patterns for BYOM |
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
| `PIPECAT_ENABLE_OBSERVERS` / `PIPECAT_ENABLE_TRACING` / `PIPECAT_TRACE_INCLUDE_PII` | No | Observability toggles (all default `true`). PII = caller phone numbers on spans. See `adr/0010` |
| `API_KEY_HASH_SECRET` | Prod | Pepper for the HMAC-SHA256 hashing of API keys — a DB leak alone can't brute-force keys without it. **Set a strong value once and don't rotate** (rotating invalidates peppered keys; pre-pepper keys keep working via dual-read + upgrade-on-use). Default `change-me-in-production` gives no real protection until set |
| `PROJECT_PURGE_RETENTION_DAYS` | No | Days a soft-deleted project (ADR-0011) is kept before the hourly purge job hard-deletes it (cascade). Default `30`; `0` disables |
| `PLATFORM_API_KEY` | Prod | Privileged credential gating the unauthenticated bootstrap endpoints — project creation + first-API-key creation. Only the builder holds it; presented as the `X-Platform-Key` header. Empty default fails **closed** (rejects all bootstrap calls), so set it wherever those endpoints must work. TurnCall stays identity-free — this is a caller check, not a user |

## Providers (per-agent config)

| Role | Providers |
|------|-----------|
| STT | `deepgram` (default, streaming), `openai`, `elevenlabs`, `cartesia` (Ink, streaming) |
| LLM | `openai` (default), `anthropic` (Claude), `ollama` (local), `custom_openai` (any OpenAI-compatible endpoint), `openrouter` (multi-model + `fallback_models` routing, voice only), `bedrock` (AWS-hosted Anthropic/Meta/Mistral/Amazon models — a *gateway*, not a vendor; credentials come from the agent's `aws` block, and `llm.extra` passes through to `additionalModelRequestFields` for e.g. Anthropic extended thinking. See `adr/0016`). Sampling: `llm.temperature` (0–2, default 0.7) + `llm.max_tokens` (default 1024) apply on voice and chat/SMS; the voicemail classifier stays pinned at 0.1. `llm.reasoning_effort` (`minimal`\|`low`\|`medium`\|`high`, unset by default) sent via `extra_body` on voice + chat/SMS — OpenAI-family only (openai/openrouter/custom_openai), for reasoning models (o-series/gpt-5); classifier forces it off. See `adr/0014` |
| TTS | `deepgram` (default), `openai`, `elevenlabs`, `cartesia` (Sonic-3.5, streaming, 60+ emotions) |
| S2S | `openai` (Realtime API), `google` (Gemini Live), `aws` (Amazon Nova Sonic 2, `amazon.nova-2-sonic-v1:0`; `voice` defaults to `matthew`, `s2s.extra.endpointing_sensitivity` = `LOW`\|`MEDIUM`\|`HIGH`. Sessions roll over every ~6 min and credentials are re-resolved each time. See `adr/0016`) — set `pipeline_mode: "s2s"`. Sampling: `s2s.max_tokens` (both), `s2s.temperature` (google only — Realtime GA rejects it with 422). `openai` accepts an optional `s2s.base_url` (`wss://`) to target an OpenAI-Realtime-compatible gateway (Vercel AI Gateway, LiteLLM) or xAI direct — routes models like `xai/grok-voice-think-fast-1.0` over the same protocol. SSRF-gated by `BYOM_ALLOWED_URL_PATTERNS` |

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
inbound message → session lookup/create → message history → LLM chat completion → store reply → respond
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
  → Store metadata + knowledge_context on call record
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
Require `webhook_url`. TurnCall POSTs: `{tool_name, arguments, call_id, project_id}`.
Optional `webhook_secret`: when set, each POST is HMAC-signed (`X-TurnCall-Signature: v1=<hex>`,
`X-TurnCall-Timestamp`, HMAC-SHA256 over `"{timestamp}.{body}"` — same scheme as event webhooks).

### Tool Schema
```json
{
  "name": "snake_case_name",
  "description": "When/why the LLM should invoke this",
  "parameters_schema": {"type": "object", "properties": {...}, "required": [...]},
  "execution_mode": "sync",
  "webhook_url": "https://...",
  "webhook_secret": "optional — HMAC-sign tool POSTs",
  "timeout_seconds": 10,
  "max_retries": 1
}
```

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
- `services/mcp_client.py` — `MCPSessionManager`: connect, discover, call, cleanup
- `orchestrator/tool_bridge.py` — Routes MCP tool calls through MCP client
- `orchestrator/pipeline_factory.py` — Merges MCP tools into pipeline at creation
- `webhooks/media_stream.py` — MCP discovery before pipeline start

### Tool Invocation Recording
All tool calls (webhook + MCP + builtin) recorded in `tool_invocations` table with: input, output, status, latency_ms.
Query via: `GET /v1/tools/invocations/{call_id}`

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
  "agent_id": "uuid | null",     // resolved from the call's active agent (handoff-aware)
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
`customer_did_not_answer`, `customer_busy`, `voicemail`, `transferred`,
`pipeline_error`, `telephony_failed`, `unknown`.

Key files: `events/webhook_delivery.py` (envelope + signing + retry),
`events/dispatcher.py` (agent_id/event_id resolution), `domain/call_state.py`
(`infer_ended_reason`). See `adr/0007`, `adr/0008`.

## API Convention

- Response: `{"success": true, "data": ...}`
- Auth: `Authorization: Bearer tc_...`
- Errors: `{"success": false, "error": "...", "code": "..."}`
- Project-scoped: all queries filtered by API key's project
- TwiML: single `callId` param + `statusCallback`
