# TurnCall

Production voice agent platform. API-only backend for real-time AI voice agents over Twilio and Telnyx PSTN, WebRTC browser calls, WhatsApp Business (voice + text), and SMS/chat text conversations.

## Stack

- **Runtime**: Python 3.12, FastAPI, Pipecat 1.12
- **Database**: PostgreSQL (asyncpg + SQLAlchemy async + Alembic)
- **Cache**: Redis
- **Telephony**: Twilio Voice + Media Streams (WebSocket) · Telnyx Call Control + media streaming (WebSocket)
- **WhatsApp**: WhatsApp Cloud API + Pipecat WhatsAppClient (voice calls + text messages)
- **SMS/Chat**: Twilio SMS + Chat API (text conversations with session management)
- **WebRTC**: Pipecat SmallWebRTCTransport (browser calls)
- **Video Avatar**: HeyGen LiveAvatar via Pipecat HeyGenVideoService (WebRTC + cascade only)
- **STT**: Deepgram Nova-3 (streaming, the `nova-3-general` default) · OpenAI Whisper · ElevenLabs Scribe · Cartesia Ink · AssemblyAI Universal-Streaming
- **LLM**: OpenAI GPT-4o-mini · Anthropic Claude (Sonnet, Haiku, Opus) · Ollama (local models) · Google Gemini · OpenRouter (multi-model + fallback routing) · Any OpenAI-compatible endpoint
- **S2S**: OpenAI Realtime · Gemini Live (native audio-in/audio-out)
- **TTS**: Deepgram Aura-2 · OpenAI TTS-1 · ElevenLabs Flash v2.5 · Cartesia Sonic-3.6
- **VAD**: Silero (barge-in / interruption handling)
- **Turn Detection**: Smart Turn V3 (ML-based, local ONNX)
- **Voicemail**: Pipecat VoicemailDetector (1.12: one processor over an `LLMClassifier`) with retry backoff
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
  campaigns/       # Campaign worker: dialler pass, process entrypoint, wake-up list
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
| `TWILIO_ACCOUNT_SID` | Yes* | Twilio account SID — the platform account. *A number bound with its own `twilio_account_sid` + `twilio_auth_token` uses those instead |
| `TWILIO_AUTH_TOKEN` | Yes* | Twilio auth token (platform account; see above) |
| `TELNYX_API_KEY` | No | Telnyx API key, the platform account for Telnyx numbers (ADR-0022). A project's `telnyx` credential replaces it **and** `TELNYX_PUBLIC_KEY` whole. Without one at any level a Telnyx call cannot be answered: it fails with `missing_keys: ["telnyx"]` |
| `TELNYX_PUBLIC_KEY` | With Telnyx | The platform Telnyx account's webhook public key (base64 Ed25519, Mission Control > Keys & Credentials > Public Key). Not a secret, but required: a Telnyx webhook nothing can verify is refused — there is no unsigned dev mode on Telnyx |
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
| `GOOGLE_API_KEY` | No | Google API key — one account for the `google` LLM and Gemini Live S2S |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (Claude LLM). Not required if using other providers |
| `CARTESIA_API_KEY` | No | Cartesia API key (Sonic TTS + Ink STT). Not required if using other providers |
| `ASSEMBLYAI_API_KEY` | No | AssemblyAI API key (Universal-Streaming STT). A project's `assemblyai` credential wins |
| `OPENROUTER_API_KEY` | No | OpenRouter API key (multi-model LLM + fallback routing). Platform-level key; per-agent `api_key` overrides. From openrouter.ai/keys |
| `HEYGEN_LIVE_AVATAR_API_KEY` | No | LiveAvatar API key for the HeyGen video avatar. From app.liveavatar.com (NOT a HeyGen key — Pipecat targets the LiveAvatar API) |
| `WHATSAPP_TOKEN` | No | WhatsApp API access token (Meta Developer Console > WhatsApp > API Setup) |
| `WHATSAPP_PHONE_NUMBER_ID` | No | WhatsApp Business phone number ID |
| `WHATSAPP_APP_SECRET` | No | Meta App Secret for webhook signature validation (App Settings > Basic > App Secret, 32 hex chars) |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | No | Arbitrary token for webhook URL verification handshake |
| `STORAGE_BACKEND` | No | Object storage backend: `local` (default) or `s3` |
| `LOCAL_STORAGE_PATH` | No | Local storage directory (default: `./storage`) |
| `AMBIENCE_CACHE_DIR` | No | Where a call's ambience renditions are fetched to before the mixer reads them (default `./storage/ambience-cache`). **Mount it** — a container recreate otherwise re-fetches every sound. No eviction: bounded by the number of distinct sounds. See `adr/0021` |
| `S3_BUCKET_NAME` | No | S3 bucket for file storage (required if STORAGE_BACKEND=s3) |
| `AWS_REGION` | No | Default AWS region for S3 **and** the `bedrock`/`aws` providers (default: `us-east-1`). Per-agent `config.aws.region` overrides it — Bedrock model availability is region-specific and rarely matches your bucket. See `adr/0016` |
| `AWS_AGENT_CREDENTIALS_ENABLED` | No | Allow per-agent **static** AWS keys in the agent config (default `false`). They persist in `config_blob`, which is unencrypted JSONB, so agents supplying them are rejected at create unless this is on. The unrestricted path is `config.aws.role_arn`, which stores no durable secret. **Agent config only**: an `aws` project credential is encrypted and not gated by it, so `false` means "no static AWS keys *in agent config*", not "none on this deployment". See `adr/0016`, `adr/0020` |
| `CREDENTIAL_ENCRYPTION_KEY` | With credentials | Fernet key for project credentials (ADR-0020). Storing one without it is an error, and startup **fails** (API and eval worker) when credentials exist and it is unset or wrong. **Set once, never rotate** — rotating orphans every stored credential |
| `PUBLIC_BASE_URL` | No | Public https base URL (e.g. `https://abc.ngrok.io`) for Twilio callbacks issued without an inbound request — warm-transfer briefing + no-answer fallback. Cold transfer works without it. See `adr/0009` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | OTLP collector endpoint for OpenTelemetry traces (e.g. `http://localhost:4318`). Required for tracing in production (it self-disables without one). `OTEL_EXPORTER_OTLP_PROTOCOL` (`http/protobuf`\|`grpc`), `OTEL_EXPORTER_OTLP_HEADERS` also honored. See `adr/0010` |
| `PIPECAT_VAD_CONFIDENCE_THRESHOLD` | No | Silero VAD confidence (default `0.6`). Pairs with the agent's `silence_timeout_ms`, which sets the VAD stop window when Smart Turn is off (with it on, the model decides the turn and VAD uses Pipecat's 0.2s — the two waits are serial, so charging both cost 1.8s a turn) |
| `PIPECAT_ENABLE_OBSERVERS` / `PIPECAT_ENABLE_TRACING` / `PIPECAT_TRACE_INCLUDE_PII` | No | Observability toggles (all default `true`). PII = caller phone numbers on spans. See `adr/0010` |
| `API_KEY_HASH_SECRET` | Prod | Pepper for the HMAC-SHA256 hashing of API keys — a DB leak alone can't brute-force keys without it. **Set a strong value once and don't rotate** (rotating invalidates peppered keys; pre-pepper keys keep working via dual-read + upgrade-on-use). Default `change-me-in-production` gives no real protection until set |
| `EVAL_TTS_CACHE_DIR` | No | Where the caller's synthesized turns are cached in audio runs (default `./storage/eval-tts-cache`). Pipecat's own default lives under `$HOME`, which a container loses on recreate — every repeat run would then re-synthesize every caller turn. Mount it |
| `EVAL_MAX_CONCURRENT_RUNS` | No | Scenario-iterations the eval worker runs at once (default `4`). The worker runs the bot pipeline **and** the harness — which itself runs a persona LLM, a TTS, an STT and the judge — so in audio mode one run is roughly double a real call's service load in one event loop. A starting point, not a measurement. `EVAL_MAX_RUN_DURATION_SECONDS` (default `900`) is the janitor's cutoff for reclaiming a run a crashed worker left claimed, as `errored`; `EVAL_MAX_QUEUED_SECONDS` (`3600`) is the same cutoff for a run **nothing ever claimed** — the API committed the row then died before the queue push — and is longer, because waiting behind a backlog is normal where running for 15 minutes is not. `EVAL_JANITOR_INTERVAL_SECONDS` (`60`) bounds the sweep. One request's paid LLM work has **two** axes and needs both: `EVAL_MAX_ITERATIONS` (`50`) per run, and `EVAL_MAX_SCENARIOS_PER_REQUEST` (`50`) on a `tag`'s fan-out — a tag matching more than that is refused naming the count, never truncated, because a batch that ran 50 of 200 scenarios reports a verdict for a suite that never ran. See `adr/0018` |
| `EVAL_JUDGE_PROVIDER` | No | What decides every verdict, set once for the platform (#119) — one of `ollama` (pipecat's own, the default when nothing is set), `openai`, `anthropic`, `jev` (#141: TypeSafe's classifier — a few hundred ms per verdict with a *calibrated* probability, judge-only, needs `TYPESAFE_API_KEY` + the `evals-jev` extra, and supplies no prose so the **explainer** comes from this same setting). With `EVAL_JUDGE_MODEL` (that provider's own default when empty) and `EVAL_JUDGE_TEMPERATURE` (unset by default — a verdict that varies run to run is not a verdict; dropped entirely for Anthropic and the OpenAI reasoning families, as on the call path). `EVAL_SIMULATOR_PROVIDER` / `_MODEL` / `_TEMPERATURE` are the same three for the LLM that plays the caller in a simulation. A **scenario's** own `judge:`/`simulator:` block wins, taken whole; a `judge` on a **run** request is a 422, since a run is what it was queued as |
| `PROJECT_PURGE_RETENTION_DAYS` | No | Days a soft-deleted project (ADR-0011) is kept before the hourly purge job hard-deletes it (cascade). Default `30`; `0` disables |
| `TYPESAFE_API_KEY` | No | Jev, for a `jev` eval judge (#141). A fallback: a project's `typesafe` credential wins (ADR-0020). Evals only — no live call contacts this vendor, which is why the dependency is the optional `evals-jev` extra rather than a base one. Neither the key nor the extra is checked at scenario create: both are properties of a *deployment* and a stored scenario outlives the one it was written against, so the **run** fails, naming which of the two is missing |
| `LIMIT_LIVE_CALLS` | No | Platform default for a project's live-call concurrency limit (#224), `0` (default) = unlimited. A project's own value (`PUT /v1/projects/{id}/limits`) wins in either direction — a default, not a ceiling. See "Limits" |
| `LIMIT_OUTBOUND_CALLS_PER_SECOND` | No | Platform default for how many outbound calls each of a project's numbers may start per second (#225), `0` (default) = unlimited; per project the same way |
| `LIMIT_MESSAGES_PER_MINUTE` | No | Platform default for how many inbound messages one text session (chat, SMS, WhatsApp) may send per minute (#226), `0` (default) = unlimited; per project the same way |
| `PLATFORM_API_KEY` | Prod | Privileged credential gating the unauthenticated bootstrap endpoints — project creation + first-API-key creation. Only the builder holds it; presented as the `X-Platform-Key` header. Empty default fails **closed** (rejects all bootstrap calls), so set it wherever those endpoints must work. TurnCall stays identity-free — this is a caller check, not a user |

## Providers (per-agent config)

| Role | Providers |
|------|-----------|
| STT | `deepgram` (default, streaming), `openai`, `elevenlabs`, `cartesia` (Ink, streaming), `assemblyai` (Universal-Streaming, #203). `stt.model` empty = that provider's own default (`nova-3-general` / `gpt-transcribe` / `scribe_v1` / `ink-whisper` / `universal-streaming-english`), resolved in the pipeline factory the way `_tts_model_voice` already does for TTS. It used to default to `nova-3-general` for **every** provider, which the other three answer 400 to — so that value is read as unset on them, since `model_dump()` persisted it into existing agents. `stt.keyterms` is one list of vocabulary hints — product names, SKUs, surnames — mapped to each provider's own spelling: `keyterm` on Cartesia, `keywords` on OpenAI, `keyterms` on ElevenLabs, `keyterms_prompt` on AssemblyAI, and on Deepgram **whichever the model takes** — `keyterm` for `nova-3*`/`flux*`, `keywords` for everything older. Deepgram 400s on the wrong one (`INVALID_QUERY_PARAMETER`) rather than ignoring it, so a hand-written `extra: {"keyterm": ...}` on a Nova-2 agent kills the call at connect; `keyterms` beats the same key in `extra` for that reason. Cartesia honors them on `ink-2*`/`ink-preview*` only and caps a connection at 100 terms / 1200 characters, truncating past that with a warning. AssemblyAI **refuses** a session carrying more than 100, so TurnCall keeps the first 100 with a warning. Blanks and duplicates are dropped at the API boundary. AssemblyAI's default is the $0.15/hr English model rather than pipecat's (Universal-3.5 Pro, three times that); it splits its models by language instead of taking one, so an agent whose `stt.language` is not English and names no model gets `universal-streaming-multilingual` (en/es/de/fr/pt/it) — the English model transcribes Spanish as English words, with no error. Pipecat reads only its declared settings onto AssemblyAI's URL, so `stt.extra` is promoted onto them by name (`domain`, `mode`, `voice_focus`…) and a key with no such setting is warned about. It runs in pipecat's turn mode: TurnCall's VAD and Smart Turn decide the turn. **Deepgram Flux** (#204) is a `flux-*` model on `deepgram` (`flux-general-en`, `flux-general-multi`; same account and key), served by pipecat's own Flux service on `/v2/listen`, and it **owns end-of-turn**: the user aggregator gets Flux's StartOfTurn/EndOfTurn (`ExternalUserTurnStrategies`, barge-in per `interruption_enabled`), Smart Turn is not built whatever `smart_turn_detection` says (it would wait again after Flux's verdict), and `silence_timeout_ms` is not read. VAD stays at pipecat's 0.2s deciding nothing — it is what the latency observer measures from, so without it a Flux call records no `metrics.turn`. Flux's own knobs (`eot_threshold`, `eot_timeout_ms`, `eager_eot_threshold`, `min_confidence`, `language_hints`) go in `stt.extra` and are set on their fields, since Flux sends no `extra`; any other key is logged and dropped. `stt.language` is not sent (`-multi` auto-detects). `stt.failover` and `tts.failover` (#223) are the same failover for the speech roles: ordered backup STT/TTS configs, each validated and keyed exactly like `stt`/`tts` (an entry is a whole config — its `keyterms`/`pronunciations`/`language` are its own, never inherited), switched on an **unusable** error only, never switched back, recorded as `provider.failover` with `role: stt|tts`. What "the call continues" means per role is a **replay** (`orchestrator/failover.py`): the STT backup is handed the caller's speech no member transcribed yet (audio since the last final transcript, bracketed by VAD frames sent *upstream* so a segmented backup — OpenAI, ElevenLabs — hears a whole segment and nothing downstream sees a new turn), and the TTS backup the reply being spoken, from its start (a reply half-said before the failure is said again in full). Both replay the **original** frames: the switcher passes a frame id out once and observers report one once, so neither the caller's audio nor the reply's bracket is delivered twice. Refused on S2S (422). An STT group cannot mix Deepgram Flux (`flux-*`) with other models (422): turn handling is chosen at build from the primary (#204), and a switch across that line would leave every turn on pipecat's 5s timeout |
| LLM | `llm.model` empty = the provider's house model (`gpt-4o-mini` for openai, `claude-sonnet-5` for anthropic, `gemini-3.6-flash` for google). `ollama`, `custom_openai`, `bedrock` and `openrouter` have none — the model *is* the deployment there — so an unnamed one is a 422 at create rather than a dead call. It used to default to `gpt-4o-mini` for **every** provider (Anthropic: `404 not_found_error`), and `model_dump()` persisted that into existing agents, so the value is read as unset off OpenAI. `openai` (default), `anthropic` (Claude), `google` (#205: Gemini, voice + text, on the `google` account Gemini Live already uses; tools on both routes — the text dialect sends `parametersJsonSchema` and hands the model's turn back verbatim, since Gemini 3 refuses a follow-up whose function call lost its `thoughtSignature`; **Gemini 3 is sent no temperature** — Google says it loops below its own 1.0, and 0.7 is TurnCall's default, not the agent's — while pre-3 models still get one; `llm.extra` merges into the generation config last on both routes, so it sets one anyway or a `thinkingConfig`; `base_url` is a 422; works as an `llm.failover` entry), `ollama` (local), `custom_openai` (any OpenAI-compatible endpoint), `openrouter` (multi-model + `fallback_models` routing, voice only), `bedrock` (AWS-hosted Anthropic/Meta/Mistral/Amazon models — a *gateway*, not a vendor; credentials come from the agent's `aws` block, and `llm.extra` passes through to `additionalModelRequestFields` for e.g. Anthropic extended thinking. See `adr/0016`). Sampling: `llm.temperature` (0–2, default 0.7) + `llm.max_tokens` (default 1024) apply on voice and chat/SMS; the voicemail classifier stays pinned at 0.1. **Claude is sent no temperature at all, on either route** — `anthropic` direct and `anthropic.*` models via `bedrock`. Current Claude models reject it (`400 "temperature is deprecated for this model"`), which made the service unusable and ended the call. The deprecation belongs to the model, so it travels with the model rather than the endpoint; the other vendors `bedrock` fronts (Meta, Mistral, Amazon) still get one. `llm.extra` is the way to set one on an older model that still accepts it — e.g. `"extra": {"temperature": 0.3}`. The Anthropic SDK dropped `temperature`/`top_k`/`top_p` from `messages.create()`, so TurnCall routes any key it won't take by name into `extra_body` (checked against the SDK's own signature). Passing one directly used to raise `TypeError` on the first LLM turn. `llm.reasoning_effort` (`minimal`\|`low`\|`medium`\|`high`, unset by default) sent via `extra_body` on voice + chat/SMS — OpenAI-family only (openai/openrouter/custom_openai), for reasoning models (o-series/gpt-5); classifier forces it off. See `adr/0014`. `llm.failover` (#222) is an ordered list of backup LLM configs, each validated and keyed like `llm` itself: when the active LLM reports an error that leaves it **unusable** (pipecat's verdict — 401/403/400/404), a voice call moves to the next and the turn that failed is replayed there, so the caller is answered; a recoverable error (429, 5xx, timeout) does not switch, and a failed service is never switched back to. Each switch is a `provider.failover` call event `{role, from, to, error}` and an entry in `failovers` on `call.ended` and the call read; in an eval it is among the iteration's `agent_errors`. Cascade voice, plus an `openai_live` **client delegate** (#246) — that delegate is an ordinary LLM, so it is built as the same group and records the same event; refused on every other S2S agent, responses mode included (422), ignored on chat/SMS. Tools and a handoff's prompt reach every member. OpenRouter's `fallback_models` is separate and unchanged |
| TTS | `deepgram` (default), `openai`, `elevenlabs`, `cartesia` (Sonic-3.6, streaming, 60+ emotions). `tts.pronunciations` is one flat word→IPA map (`{"Metformin": "mɛtˈfɔɹmɪn"}`) — the outbound mirror of `stt.keyterms`: one spelling, each provider's own markup generated from it (Cartesia inline phonemes, ElevenLabs SSML `<phoneme>`, Deepgram inline object; OpenAI has none). Validation is **TurnCall's own**, at the API boundary: pipecat's IPA utilities normalize and tokenize but never reject, so `met-FOR-min` would be accepted, inserted into the markup and do nothing — it is a 422 naming the word. `/…/` and `[…]` are stripped and the value stored canonically, so two spellings are one config; blanks are dropped and an empty map equals absence. Whether the built voice can *read* the markup is decided at pipeline build, not at create — on ElevenLabs it takes a phoneme-capable model **and** `enable_ssml_parsing` — and is a **warning** naming the provider, never a refusal, since an agent's voice changes after its pronunciations are written. That flag is a constructor kwarg no `tts.extra` could reach, so the pipeline factory passes it, for exactly the agents that have pronunciations *and* a model pipecat lists in `ELEVENLABS_PHONEME_MODELS` — never for everyone, since SSML parsing changes how **all** of that agent's text is read (#147). The model stays the author's: a phoneme-incapable one is warned about, never downgraded — and the markup is **withheld** from it, because a `<phoneme>` tag reaching a service that is not parsing SSML is *spoken aloud* (measured on the WebSocket: 3.5-4.3x the audio), which is worse than the mispronunciation. One unusable word is spoken as written; it never silences the reply. Two things that cannot be refusals ride back as **`warnings` on the agent read** instead (#148, derived every read, never stored, the shape #95 set for eval scenarios): a "pronunciation" equal to its own spelling (`{"Metformin": "metformin"}` — legal IPA *notation*, so the 422 cannot see it, and it reaches the voice as real markup), and a voice that will not read the markup, which #147 knew at build and said only to a log. The transform is registered **last** (a later transform would eat the markup — markdown stripping and an SSML tag). Cascade only: an S2S agent has no TTS stage and is warned at build. `tts.failover`: see the STT row (#223). |
| S2S | `openai` (Realtime API), `openai_live` (GPT-Live-1, full duplex; it calls **no tools itself** and hands them to a *delegate* — `s2s.delegation` `{mode, model, reasoning_effort, service_tier, instructions, timeout_seconds}`. `client` (#244, the **default** for an agent with static or MCP tools, or a `tool`-mode KB, and no block; none without tools) builds the agent's own `llm` the way cascade does and runs it in pipecat's `BackendLLMWorker` with the tools and TurnCall's handlers on it — the live service's context carries none; `timeout_seconds` (default 30) bounds each delegation, and the delegate's account joins `missing_keys`. Call control works in client mode too (#245): the worker is outside the call's pipeline, so `end_call`/`do_not_call` reach the call's `HangupAfterReply` directly (`register_tools(..., hangup_gate=)`) instead of pushing from the delegate's LLM, inside the same shielded record. One goodbye (#262): gpt-live-1 used to answer the caller's "bye" itself and then voice the delegate's goodbye too, since the delegate's transcript ends where its delegation began. So in client mode the delegation paragraph tells gpt-live-1 to leave the goodbye to the delegation (`GOODBYE_TO_THE_DELEGATE`, only with `end_call`), and the delegate's tool only *marks* the call as ending (`begin_ending`): the live service arms the gate as it relays the delegate's answer, once gpt-live-1 is quiet, so a filler it said meanwhile ("One moment") is not taken for the goodbye. With nothing of the delegate's to relay — an empty or failed answer, whose pipecat stand-in sentence then goes as thinking rather than commentary — the call ends after the reply playing (`HangupArmFrame(current_reply=True)`). `model`/`reasoning_effort`/`service_tier` are a 422 in client mode. `responses` (#243) is an OpenAI-hosted Responses model whose function calls the live service runs with TurnCall's handlers in the call's pipeline, so `end_call`'s hangup gate works; `timeout_seconds` bounds each such call. gpt-live-1's instructions gain a generated paragraph naming every tool; `system_prompt` reaches both, `delegation.instructions` replaces the delegate's. `s2s.extra.backend_model` is read as responses mode with that model. 422: `delegation` on any other provider, responses without `model`, `handoff_to_agent` among its tools. It waits for speech, so its session opens on a trailing developer message — the `first_message`, or a greeting — which pipecat sends as commentary once the session starts, the API's way of asking it to speak first (#248, `s2s_opening_messages`); every other provider opens on the empty context), `google` (Gemini Live, `gemini-3.8-live`; `voice` defaults to `Charon`), `aws` (Amazon Nova Sonic 2, `amazon.nova-2-sonic-v1:0`; `voice` defaults to `matthew`, `s2s.extra.endpointing_sensitivity` = `LOW`\|`MEDIUM`\|`HIGH`. Sessions roll over every ~6 min and credentials are re-resolved each time. See `adr/0016`) — set `pipeline_mode: "s2s"`. Knowledge bases: `prompt` + `tool` on every provider, `auto` warned about and skipped (#254, see Knowledge Base). Sampling: `s2s.max_tokens` (both), `s2s.temperature` (google only — Realtime GA rejects it with 422). `openai` accepts an optional `s2s.base_url` (`wss://`) to target an OpenAI-Realtime-compatible gateway (Vercel AI Gateway, LiteLLM) or xAI direct — routes models like `xai/grok-voice-think-fast-1.0` over the same protocol. SSRF-gated by `BYOM_ALLOWED_URL_PATTERNS`. `s2s.model`/`s2s.voice` default to OpenAI's (`gpt-realtime-2.1`/`alloy`) whatever the provider, so each non-OpenAI path swaps those sentinels for its own — and passes an explicitly chosen value through untouched, so a wrong one is reported by the provider rather than silently replaced |

## Project credentials

A project's own provider keys (ADR-0020), one per provider **account** — the
namespace is `domain/providers.py` (`openai_live`→`openai`, `bedrock` and S2S
`aws`→`aws`; `ollama`/`custom_openai` have none). Carriers are the `carrier`
role: `telnyx` is an account (`{"api_key", "public_key"}`, both required — the
public key verifies that account's webhooks); `twilio` is not, its account
rides on the number.

```
PUT    /v1/credentials/{provider}   # {"api_key": "..."}, or the AWS block's shape for `aws`
GET    /v1/credentials              # presence + timestamps, never a value
DELETE /v1/credentials/{provider}   # idempotent; the project falls back to the platform key
GET    /v1/providers                # roles -> providers; per account platform_key + project_credential
```

Every provider key resolves through `services/credentials.Credentials`: agent
inline (`llm.api_key`, `config.aws`) → project credential → platform env. One
order everywhere a project's work runs: the voice pipeline
(`pipeline_builder.load_call_credentials`), chat/SMS/WhatsApp text turns,
post-call analysis and takeaways, the transfer briefing, KB upload/search
embeddings (`credentials.for_project`), and the eval judge (via
`credentials.scope`, since pipecat builds it from a factory path). Read fresh
each time, so writes apply to the next call or message. So a deployment can run
with every provider key in `.env` and no project credentials, or with none in
`.env` and every key per project — or any mix. `custom_openai` never receives a
project's OpenAI key (its endpoint is the agent's own). No code reads
`os.environ` for a key.

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

## Carriers

A **carrier** is the telephony company a number belongs to (ADR-0022): Twilio
or Telnyx, with Asterisk ARI next. The boundary is cut for them:

- **`TelephonyAdapter` is REST actions only** — dial, hang up, transfer, DTMF,
  configure a number, SMS. How a carrier answers a call (TwiML) and signs its
  webhooks (Twilio's HMAC) belong to its edge: `generate_media_stream_twiml`
  lives in `webhooks/twilio_handlers.py`.
- **Each carrier owns its inbound edge** — routes, signature check, answer
  format, and its media stream's handshake (`webhooks/twilio_media_stream.py`
  for Twilio's `/ws/media-stream`). Every edge hands the open stream to **one
  shared media entry point**, `webhooks/media_stream.run_media_stream`, which
  picks the serializer from the **call's** carrier (`calls.provider`) through
  `transport_factory.CARRIER_MEDIA`. A call whose carrier has no entry there is
  refused before anything is built. Past the serializer nothing learns the
  carrier.
- **The API says `carrier`.** Phone-number reads carry `carrier` (and the same
  value as `provider`, deprecated); writes take `carrier`, or `provider` as a
  deprecated alias — both at once must agree. The columns keep the name
  `provider`. A number's carrier is fixed at bind: a `PUT` naming another is a
  422. `domain/carriers.CARRIERS` is the closed set.
- **Carrier contract test**: `tests/integration/test_carrier_contract.py` runs
  one agent and one caller script per carrier (Seam A) and holds each to the
  same outcome, events and transfer request. A new carrier adds its edge to
  `CARRIERS` there; `tests/unit/test_carrier_boundary.py` fails until it has a
  `CARRIER_MEDIA` entry. Telnyx runs the inbound case; its transfers join with
  #220.
- **The shared middle of an inbound call** — resolve the agent (call-init, A/B
  weights, the bound agent), create the call row on its carrier, record
  `call.initializing` + `call.started` — is `services/inbound_call
  .open_inbound_call`, for every carrier's edge. An edge only answers numbers
  bound on its own carrier.
- **Call control acts on the call's carrier**: `services/call_control
  ._carrier_adapter` picks the adapter (and account) by `calls.provider`.
  `services/twilio_accounts.TwilioAccount.adapter` and
  `services/telnyx_accounts.TelnyxAccount.adapter` are the only places each
  carrier's adapter is built — the harness's `FakeCarrier` replaces both.

### Telnyx (Call Control, #217)

```
Telnyx POSTs call.initiated → /webhooks/telnyx/voice (Ed25519-signed)
  → verify with the key of the project the call/number belongs to
  → open_inbound_call (carrier "telnyx") → REST `answer` with stream_url
  → Telnyx opens /ws/telnyx/media-stream/{call_id} → run_media_stream
     (pipecat TelnyxFrameSerializer, PCMU both ways)
  → stream ends → REST `hangup`; call.hangup is recorded on the event log
```

- **Signature**: `telnyx-signature-ed25519` over `{telnyx-timestamp}|{raw
  body}`, five-minute window (Telnyx's SDK scheme), in
  `webhooks/telnyx_signature.py`. The key is the project's `telnyx`
  credential's `public_key` when the call or number belongs to a project that
  holds one, else `TELNYX_PUBLIC_KEY`. No key, bad signature, stale timestamp:
  403, nothing created.
- **The edge hangs up.** On Twilio the stream closing ends the call; on Telnyx
  it does not — the line would stay up, silent and billed. So the media stream
  edge sends `hangup` when the stream ends (pipecat's serializer hang-up is off:
  REST commands are the adapter's). A call the caller already ended answers
  `90018`, which counts as hung up. #220 must skip this for a transferred call.
- **The stream must be the call's**: its `start.call_control_id` has to be the
  call's `provider_call_sid`, or it is closed — no pipeline, no hang-up.
- **`call.hangup`** is stored as a `call.ended` event (as Twilio's `completed`
  status callback is) and finalizes only a call no media stream ever opened
  for; one that has a pipeline is finalized by it, recording complete.
- **A missing key** (no `telnyx` credential, no `TELNYX_API_KEY`) fails the call
  before answering: `error.raised {stage: "carrier_answer", missing_keys:
  ["telnyx"]}`, `call.ended` failed. A key Telnyx refuses (401/403) carries
  `provider: "telnyx", key_rejected: true`.
- **Binding** (#219): `carrier: "telnyx"` on `POST /v1/phone-numbers` looks the
  number up on the project's Telnyx account (`external_number_sid` optional; a
  number it cannot see is a 422, no key at any level a 503
  `no_telnyx_account`) and routes it: Telnyx routes by Call Control
  application, so `TelnyxAdapter.configure_number_webhook` puts the number on
  this deployment's application `TurnCall <PUBLIC_BASE_URL host>` — created
  with webhook format **2** (Telnyx's default 1 is a shape the handler cannot
  read) and the account's outbound voice profile if it has exactly one,
  corrected on every bind/update — and `voice_url=""` takes it off. A TurnCall
  application left empty (the last unbind, or the old host after
  PUBLIC_BASE_URL moved) is deleted; a connection not named `TurnCall ` is
  never touched. Unbind is best-effort, project deletion is not (as on
  Twilio). The bind/update response carries `webhooks_configured` for every
  carrier; `twilio_webhooks_configured` is null off Twilio. `sms_enabled` and
  a Twilio account on a Telnyx number are a 422 (SMS on Telnyx is out of
  scope). Live-verified: create/correct/delete of the application; the
  number PATCH (`connection_id`, null to unassign) is not — no number on the
  test account.
- **Outbound** (#218) takes the same path as Twilio's,
  `services/outbound_dial.place_outbound_call` — suppression, limits, then the
  dial — so `POST /v1/calls/outbound` and campaigns dial Telnyx numbers
  unchanged. The carrier is chosen at the adapter call. Telnyx's dial names a
  Call Control application, so the caller-id number's `connection_id` is looked
  up before the call is admitted (a number on none is a 409 naming the fix) and
  stored as `metadata_json["telnyx_connection_id"]`, where a transfer reads it
  as on an inbound call; the dial passes the call's `stream_url` up front: Telnyx opens the stream when the
  callee answers, with no answer step. Its events go to `/webhooks/telnyx/voice`
  (`webhook_url` on the dial); an outgoing call's number is its `from`. A dial
  nobody answered ends by `call.hangup`'s `hangup_cause` — `timeout`/`no_answer`
  → `no_answer`, `user_busy`/`call_rejected` → `busy`, `not_found` → `failed`
  + `call.failed` — which is what a campaign's retry policy reads.
- **Not yet**: transfer + DTMF (#220) raise `NotImplementedError` on
  `TelnyxAdapter`, naming the ticket.
  The live-call limit (#224) admits a call in `open_inbound_call`, ahead of
  call-init, for every carrier.

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
- Over the project's `messages_per_minute` (#226): not stored, no LLM; Chat API `429`, SMS/WhatsApp unanswered (see "Limits")
- Webhook events: `session.created`, `session.updated`, `session.deleted`, `chat.created`

### Phone Number Config
- `sms_enabled: true` on bind → auto-configures Twilio SMS webhook
- Same number handles both voice calls and SMS
- **Own Twilio account per number**: `twilio_account_sid` + `twilio_auth_token` on
  `POST/PUT /v1/phone-numbers` (both or neither). TurnCall looks the number up on
  that account (which proves it owns it, and makes `external_number_sid`
  optional), stores the token encrypted under `CREDENTIAL_ENCRYPTION_KEY`, and
  returns only `twilio_account_sid` + `twilio_credentials: number|platform`.
  Every Twilio REST call for that number — webhook config, outbound, hangup,
  transfer, DTMF, recording download, project deletion — and every inbound
  signature check (matched by the webhook's `AccountSid`) uses that account.
  `services/twilio_accounts.py` is the one place that decides; nothing else
  constructs a `TwilioAdapter`.

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

## Ambience

A room the agent sounds like it is in (ADR-0021): a project's uploaded sound,
mixed quietly under and between everything the agent says, for the whole call.

```
POST   /v1/ambience-sounds            # multipart upload -> one mono MP3 per transport rate
GET    /v1/ambience-sounds            # list
GET    /v1/ambience-sounds/{id}       # + rendition_urls, to hear what a caller will
GET    /v1/ambience-sounds/{id}/renditions/{rate}   # the MP3 itself — rendition_urls are file paths on local storage
DELETE /v1/ambience-sounds/{id}       # 409 naming the agents while any names it; deletes the objects too
```

```json
"ambience": {"sound": "<ambience sound id>", "volume": 0.3}
```

- **Normalized at upload, never validated.** pipecat's `SoundfileMixer` is
  silent on a wrong rate (mixes nothing) and on stereo (double speed), so every
  upload is decoded, forced to mono and rendered per rate in
  `transport_factory.VOICE_OUTPUT_RATES` — the constant every voice transport
  builds from. 10 MB (409, checked on `Content-Length` and the bytes), 60 s
  (409 — the mixer holds the file per call), 415 on anything libsndfile cannot
  decode, naming the formats and the `.m4a` fix.
- **The upload warns when a phone won't carry it** (`inaudible_on_phone`,
  advisory — the sound is stored either way). A sound can play and be silent:
  a phone line carries 300–3400 Hz, and a low rumble lives below it. Measured on
  a real call — a synthetic rumble at −57 dBFS in-band was inaudible where a
  restaurant recording at −29 was clear. The threshold is −50 dBFS in-band.
- **The sound id resolves against the call's project**, for stored and inline
  agents alike (#173). Stored agents are also checked at save (422).
- **Every voice transport, S2S included; never evals.** The mixer lives in the
  transport, so there is no TTS stage to gate on. An eval run carries an
  `ambience_skipped` warning instead. Every entry point hands its voice
  transport a `mixer=` — `tests/unit/test_ambience_wiring.py` fails on one
  that does not.
- **A streamed voice is judged before the mix** (#265). gpt-live-1 and the
  video avatars stream silence too, and pipecat finds the end of a reply by
  that silence *after* mixing the room in — so the bot never stopped speaking
  and the idle guard never armed. `SpeechBeforeAmbience`, right before the
  output, decides speech or silence first and ends a reply with a
  `TTSStoppedFrame`. `test_pipecat_still_needs_it` fails once pipecat is fixed.
- **It can never end a call.** Missing sound, missing rendition, failed fetch:
  a warning, and a silent room.
- In the call recording (the agent's channel carries the room, as the caller
  heard it) and still playing under the voicemail gate — both on purpose, see
  the ADR.

Key files: `services/ambience_ingest.py`, `api/v1/ambience.py`,
`orchestrator/ambience.py`, `storage/repositories/ambience_repo.py`.

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

Knowledge bases are links on an agent row, so an inline config names its own in
`knowledge_bases` (#260, the link's fields). `pipeline_builder._assemble` reads
them only when `agent_id` is the sentinel, resolving each against the call's
project (`load_inline_kb_attachments`; a miss is a warning and a skipped KB,
never a failed call), then carries on exactly as for links. A stored agent's
come from its links alone: the agent endpoints 422 the field, and
`AgentConfigSchema` never dumps it into a `config_blob`.

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
DELETE /v1/knowledge-bases/{kb_id}                      # Delete KB + its files (blocked if agents depend on it)

POST   /v1/knowledge-bases/{kb_id}/documents            # Upload document (multipart)
GET    /v1/knowledge-bases/{kb_id}/documents            # List documents
GET    /v1/knowledge-bases/{kb_id}/documents/{doc_id}   # Get document
DELETE /v1/knowledge-bases/{kb_id}/documents/{doc_id}   # Delete document + chunks + stored file

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

### S2S agents

`prompt` and `tool` work on every S2S provider (#254): the preamble leads the
instructions, and `query_knowledge` is one more tool — on the realtime service,
or on gpt-live-1's delegate (every failover member), counting as a tool for the
client-mode default and `missing_keys`. `auto` has no per-turn point ahead of a
realtime model: `pipeline_builder` drops it **before** the preamble (its
awareness hint would claim a KB the model cannot read) with a warning, and the
agent read says the same — `config_warnings.knowledge_mode_warning` is the one
rule both use. The links live outside the config, so the agent endpoints load
their modes (`knowledge_repo.kb_modes_by_agent`, one query per page).

### Inline agents

An agent with no row — call-init `agent`, an eval `inline` target — names its
KBs in `config.knowledge_bases` (#260); see "An inline agent has no agent row".
On a stored agent that field is a 422 naming the link endpoint.

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

## Live Call Control

`POST /v1/calls/{id}/end|transfer|dtmf` act through the carrier's REST API.
`messages` and `handoff` change the call's pipeline, which only the
task running the call can do — so they go through the call (#215, tracker
#198), over a per-call Redis pub/sub channel, `call:{id}:control`
(`services/live_call_channel.py`):

- `CallSession.start()` subscribes before the call reads as live and drops it
  when the call stops running, so every voice transport has it (Twilio,
  WebRTC, WhatsApp all end there; `test_live_control_wiring.py` fails on an
  entry point that does not). None in an eval. Its connection is its own,
  outside the shared pool — a subscription holds it for the whole call. A
  Redis that cannot be reached costs the call its channel, never the call.
- The API publishes `{kind, body, id}`. `PUBLISH`'s receiver count of **0 is a
  409 `call_not_live`**, never a success; the call answers on a list keyed by
  the request id, and **none within `ACK_TIMEOUT_SECONDS` (2s) is a 504
  `call_did_not_acknowledge`**. A handler that raises `ControlRefused` is a 409
  with its reason. Handlers run one at a time per call, in arrival order.
- `message`: appended to the context (`run_llm=False`), read on the agent's
  next turn. Cascade only — a realtime model reads no context added after it
  starts, so S2S refuses it rather than answer 200 for a message nothing reads.
- `handoff` (#216): the call runs `tool_bridge.run_handoff` — the model's
  `handoff_to_agent` path, prepare / commit / apply (#144) — on its own LLM,
  context and worker (`live_control.stages_of`), so the record and the pipeline
  agree. The 200's `details` is what it applied: `target_agent_id`,
  `target_assistant_name`, and the `tools` it now advertises. 404 for an agent
  outside the project; 409 on `openai_live` (refused like the tool), when the
  call is not `in_progress`, or when the target cannot be loaded. The API never
  calls `call_control.handoff_to_agent` itself (`test_live_control_wiring.py`).
  Nothing is said on the switch: the new agent answers the caller's next turn,
  over the cleared context the tool path leaves too.
- Also the cross-replica path: whichever replica holds the call answers.

## Suppression List

A project's numbers that must never be dialled (#227). Checked in
`services/outbound_dial.place_outbound_call`, the one path every outbound call
takes, **before** the call row is written — a suppressed `to_number` is `409
number_suppressed` and the carrier is never asked. Numbers are E.164, the same
pattern `to_number` is validated against, so they compare as strings. No
consent records: consent is the operator's.

```
POST   /v1/suppression-list        # {"number": "+1..."}; re-adding returns the existing entry
GET    /v1/suppression-list        # ?page= ?limit= ?number= (finds one entry's id)
DELETE /v1/suppression-list/{id}
```

## Graceful Shutdown

A deploy must not drop live calls (#199). Uvicorn's own SIGTERM closes the
listening socket and every connection at once — a Twilio media stream gets
close code 1012 — so `services/drain.py` takes SIGTERM over in the lifespan
(uvicorn installs its handler *before* the lifespan runs, so ours replaces it)
and only hands the signal back once the process is empty:

- `/ready` is `503 {"status": "draining", "active_calls": n}` from the first
  SIGTERM; `/live` stays `200` throughout (a liveness failure would kill the pod
  mid-drain). The process keeps serving, late arrivals included.
- It waits for live calls, each bounded by its own `max_call_duration_seconds`
  + 30s from when it was counted, then up to 60s for post-call work
  (`pending_post_call_tasks`, `pending_deliveries`) so `call.ended` goes out.
  Then `signal.raise_signal(SIGTERM)` with uvicorn's handler restored; the
  process exits by the signal (-15 / 143). A second SIGTERM hands back at once.
- A call is counted by `drain.live_call(...)` at the transport entry points —
  `webhooks/media_stream.py` (Twilio) and `pipeline_builder.start_call_pipeline`
  (WebRTC, WhatsApp voice) — from config resolution through finalize. A new
  voice entry point must count its calls the same way.
- `GET /metrics`: `turncall_active_calls`, per process, Prometheus text, no
  client library.

Orchestrator contract (preStop sleep, `terminationGracePeriodSeconds` ≥ preStop
+ longest `max_call_duration_seconds` + 90): `docs/guides/graceful-shutdown.mdx`.
Proof against a real process: `tests/integration/test_graceful_shutdown.py`
(`tests/harness/server.py` boots the app as a child process).

## Limits

A **concurrency limit** caps how many are in progress at once, a **rate limit**
how many start per unit of time (CONTEXT.md); both refuse as `rate_limited`
(#206). Counted in Redis by `services/limits.py`, so they hold across servers;
per project (`PUT /v1/projects/{id}/limits`, admin key, null = the `LIMIT_*`
env default, `0` there = unlimited). Redis unreachable → admitted and logged:
a guardrail must not take the phone line down.

- **Live calls per project** (#224). Every voice call takes a slot under its
  call id *before* its row is written (`call_repo.create_call(call_id=)`):
  inbound phone calls in `services/inbound_call.open_inbound_call` (every
  carrier, #217) and WhatsApp voice in its webhook — over the limit, refused
  unanswered at the carrier (Twilio `<Reject reason="busy"/>`, Telnyx's
  `reject` command with cause `USER_BUSY`, WhatsApp `reject`) and dispatched as
  `call.rejected` with **no** `call_id` (there is no row) —
  outbound in `place_outbound_call` and WebRTC in `/connect`, both `429`.
  A slot is a **lease** (`LIVE_CALL_LEASE_MS`, 2 min) that whoever runs the call
  renews (`hold_live_call`, in `media_stream` and `start_call_pipeline`) and
  frees on any exit; the Twilio status callback frees it on any terminal status
  (an unanswered outbound call's only ending), as Telnyx's `call.hangup` does,
  and a failed dial frees it at
  once. A server that dies frees nothing — its leases lapse, so a crash costs a
  project slots for two minutes, never for good. Every call is counted even
  unlimited, so a limit set mid-day counts what is already live.
  `test_live_call_limit_wiring.py` fails on a call row written without a slot
  or a call run without a hold. Evals take none (not calls).
- **Outbound calls per second, per number** (#225). A sliding one-second
  window keyed on the from-number's E.164 (a carrier's cap is on the number),
  checked in `place_outbound_call` after the live-call slot
  (`admit_outbound_call`); refused → the slot is given back and `429` with
  `retry_after_ms`. A refused dial consumes nothing in either count.
- **Messages per minute, per text session** (#226). `admit_message` in
  `sms_chat._process_chat_message`, after the session is resolved and before
  the message is stored or reaches the LLM: the Chat API answers `429`, SMS
  answers an empty `<Response/>` and WhatsApp sends nothing. Per session, so
  one runaway conversation holds back no other.
- **`may_dial(session, project_id=, from_number=)`** answers "would a dial be admitted now?"
  without taking anything — for a campaign worker pacing itself (#229, #233);
  a `RateLimitedError` from the dial itself still means "not yet".

Tests: `tests/integration/test_live_call_limit.py` on the #214 harness, with
a second server **process** (`tests/harness/second_instance.py`) on the same
Postgres and Redis — two apps in one interpreter share every global, so they
cannot show "across servers".

## Campaigns

A set of outbound calls one agent places to a list of targets (#229,
CONTEXT.md). The API records one; `turncall-campaign-worker` — its own process,
same image, never the API's event loop (ADR-0018's reason) — dials it.

```
POST /v1/campaigns                    # {name, agent_id, from_number_id, timezone?, targets: [{number, variables?, timezone?}]}
GET  /v1/campaigns                    # ?page= ?limit=; each with target counts per status
GET  /v1/campaigns/{id}
GET  /v1/campaigns/{id}/targets       # ?status= ?page= ?limit=; per-target status, call_id, ended_reason, error
```

- **Timezone required**, IANA names only (`zoneinfo.available_timezones()`),
  on each target or as the campaign default — a target with neither is a 422.
  Never inferred from the number (a ported mobile keeps its area code). Each
  target stores its resolved zone, for #230's calling window. A number twice in
  one campaign is a 422; at most 10,000 targets a request.
- **Variables** go into the call's `metadata.template_variables`, which the
  media stream renders exactly as it renders call-init's; the metadata also
  carries `campaign_id` + `campaign_target_id`.
- **Every dial is `place_outbound_call`**, so suppression and the limits hold
  as for a single API dial. The worker asks `limits.may_dial` before claiming
  and stops the campaign's pass on "no"; a `429` from the dial itself unclaims.
- **Target states** (`domain/enums.CampaignTargetStatus`): `pending` →
  `dialling` (claimed) → `calling` (dialled; `attempts` +1, `call_id`) →
  `completed` with the call's `ended_reason` (read through `call_view`, settled
  at the next pass after the call ends), or `suppressed` / `failed` (`error`
  says why) straight from `dialling`. Every move is a guarded UPDATE from the
  state it expects. The claim (`SKIP LOCKED`, so several workers share one
  campaign) is **committed before the dial**, and the call row and the move to
  `calling` commit together: a worker that dies mid-dial leaves `dialling`, never
  a silent redial — reclaiming it is #232's janitor's call. Statuses are
  strings, not a PG enum, so #231/#232 add states without `ALTER TYPE`.
- **The clock is injected** (`campaigns.dialer.Clock`); due means
  `next_attempt_at` null or `<=` now. #230 (windows) and #231 (retry spacing)
  set `next_attempt_at`; nothing reads wall time directly.
- **The worker** (`campaigns/worker.serve`) runs a pass, then waits on a Redis
  list that `POST /v1/campaigns` pushes to, at most `CAMPAIGN_POLL_SECONDS`
  (default 2). The database is the work; the list only wakes it, so a lost push
  costs one poll. A stop is honoured between passes.

Not yet: calling windows (#230), retries (#231), pause/resume/cancel and the
janitor (#232), pacing beyond the limits (#233), weighted agents and
start/finish events (#234). Until #233, a project with no live-call limit is
dialled as fast as one pass can go.

Key files: `campaigns/dialer.py`, `campaigns/worker.py`, `campaigns/queue.py`,
`api/v1/campaigns.py`, `storage/repositories/campaign_repo.py`. Tests:
`tests/integration/test_campaigns.py` (the worker's pass against the fake
carrier; `answer_outbound_call` picks up a dialled call).

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
The vocabulary is wider than this file's examples: `definition` is stored
verbatim, so every pipecat 1.11 field is already reachable — `send_after`
barge-in, `absent`, `within_ms`, `stop_on_failure`, `function_call_stopped`,
simulation `measure:` metrics, `context:` (which **replaces** the agent's
prompt). `evals-design.md` §3 has each one as a JSON example, the five that
parse and do nothing here, and `tests/unit/test_scenario_vocabulary.py` keeping
it honest. §9.7 there records the bridge outage that kept any of it from
reporting a verdict — the eval pipeline built no `RTVIProcessor`/`RTVIObserver`,
so every run died at pipecat's `bot-ready` handshake and scored `errored`. Both
halves are wired now, eval path only; `tests/unit/test_eval_rtvi_bridge.py`
fails if either is dropped again.

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
**An `openai_live` agent never sees a scenario's `context:`** — its session opens
on the agent's own context before the harness connects (#256) and reads no later
one; the run carries a `context_ignored` warning.
The judge is pipecat's `EvalJudge`, **Ollama by default** — but a scenario can
name another (#118): `judge: {provider, model, temperature, endpoint}` and
`simulator: {...}` are TurnCall columns beside `tool_mocks`, compiled into
pipecat's blocks at parse time. `provider` is a closed set (`ollama`, `openai`,
`anthropic`, `jev`) mapping to factories **TurnCall ships**, because pipecat's own
escape hatch is `factory` — a dotted path it hands to `importlib.import_module`,
which from a request body is remote code execution. A definition naming one
anywhere pipecat reads it (`judge.eval`, `judge.eval.explainer`,
`judge.transcription`, `simulator`, `user.speech`) is refused at the API boundary.

`jev` (#141) is the one that is **judge-only**, and structurally rather than by
a rule: the allowlist is split by role — `PROVIDERS` is what a persona may be,
since it is linked into a pipeline to speak, and `JUDGE_PROVIDERS` is the
superset pipecat accepts a classifier in — so `simulator: {provider: "jev"}`
fails the existing unknown-provider check with the role in the message. It takes
no temperature (a classifier is not a sampler), and it writes no prose, so the
explainer question below is what makes it usable. Temperature has no pipecat field
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

**Who explained is TurnCall's choice too** (#139). Since 1.12 the verdict comes
from a classifier, and a second model — the explainer — writes the reason behind
every `no` and every verdict the judge was unsure of. For an LLM-backed judge
pipecat explains with the judging model itself, which is left exactly alone. For
a classifier-only judge (`jev`) there is no such model and pipecat falls back to
**its own default LLM service, a local Ollama** — so a judge pointed at a hosted
vendor would silently acquire a dependency on a service nobody provisioned, and
say so as an unreachable connection mid-run. TurnCall compiles the explainer from
the platform's configured judge instead, and where the platform has configured
none it turns reasons **off** explicitly: a verdict with probabilities and no
prose is worse than one with prose, and a verdict that *fails* on an
unconfigured service is worse than both. A raw `explainer:` inside a stored
`definition` still wins, like the rest of that block.

Which judge answered is then recorded and **acted on**. `harness_config` carries
`judge_provider` and `judge_temperature` beside `judge_service`/`judge_model`/
`judge_factory` (the first two are what the dotted path and `extra` swallowed),
plus `explainer_used`/`explainer_provider`/`explainer_model` — the absent-block
case records the judging model, because that is the model that explained,
and on `start_run` the resolved judge is compared with the last run of the same
scenario that reached a verdict — one query on `ix_eval_runs_scenario`. A
difference adds a `judge_changed` warning naming both, because a red run that
nobody can attribute to a judge swap is the expensive version of this. Only keys
the older snapshot actually recorded are compared: rows written before #119 have
no `judge_provider` and rows before #139 no `explainer_provider`, and reading
that absence as a change would announce one on every scenario's next run.

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
- `evals/runner.py` — the iteration loop, the snapshots, the budgets
- `evals/targets.py` — the three target forms; an id pins a version, a name resolves at run time
- `evals/run_warnings.py` — what a run tells its author that is not a verdict (mock that cannot fire, judge changed, stale worker)
- `evals/result_mapping.py` — one pipecat result -> one `results` entry, plus `derive_status`; the two mappers stay separate on purpose
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
| `end_call` | Terminate the call — after the agent's goodbye: records `call.ended` `source: control` and arms `orchestrator/hangup.py`, the model replies to the result (the goodbye), and the gate ends the pipeline gracefully after that reply; the transport closing is the hangup. Keyed on the reply, not the tool call, because models rarely say goodbye and call a tool in one response. The goodbye is final: input stops when it is written, so the caller is not heard (or transcribed, or recorded) while it plays. `POST /v1/calls/{id}/end` still hangs up at once | `reason` (optional) |
| `transfer_call` | Transfer to phone number (cold/warm) | `target_number` (required), `transfer_mode`, `transfer_message`, `briefing`, `fallback_message`, `reason` |
| `handoff_to_agent` | Switch to another agent. Refused (422) on an `openai_live` S2S agent | `agent_id` (required), `reason`, `context` |
| `send_dtmf` | Send keypad tones | `digits` (required) |
| `do_not_call` | Suppress the caller's number (project-wide), record `call.opted_out`, end the call after the goodbye, like `end_call` (#228). Opt-in like every built-in: listed in `tools` or absent. A call with no E.164 caller (WebRTC) is refused and keeps running | none |

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
- `services/mcp_client.py` — `MCPSessionManager`: connect, discover, call, cleanup. **mcp 2.x only** (`mcp>=2.1.1,<3`), which pipecat 1.12's `mcp` extra forces: the dual-spelling reads for 1.x's `Tool.inputSchema` / `CallToolResult.isError` are gone, since under that pin they could never match — on 2.x the old names survive only as pydantic aliases. Uses `streamable_http_client`, building its own HTTP client for headers/timeout
- `orchestrator/tool_bridge.py` — Routes MCP tool calls through MCP client
- `orchestrator/pipeline_factory.py` — Merges MCP tools into pipeline at creation
- `orchestrator/pipeline_builder.py` — `start_call_pipeline()`: MCP discovery for WebRTC + WhatsApp voice (in the task that also runs the call — MCP transports open anyio cancel scopes that must be exited where they were entered)
- `webhooks/media_stream.py` — MCP discovery before pipeline start (every carrier's media stream)
- `services/chat_tools.py` — webhook + MCP tools for text turns (per-message connect/close)
- `services/tool_webhook.py` — the shared webhook POST + HMAC signing, used by both paths
- `services/url_allowlist.py` — `check_url_allowed()`: MCP urls + BYOM/S2S base_urls share one SSRF gate (`BYOM_ALLOWED_URL_PATTERNS`; empty = allow all). Matches the whole URL **and** the host separately, so the trusted name can't be smuggled into a path, query or `user@` prefix

Tool names are flat and unique: precedence is built-in > agent `tools` > MCP (server order — claimed serially after a concurrent discovery round, so the agent's config decides, not which server answered first); a collision is skipped + logged. That order is enforced twice — in what gets advertised (`_build_tools_schema`) *and* in what actually runs (`tool_bridge`, `chat_tools`), which have to agree. `handoff_to_agent` swaps the prompt and the target's `tools` (via `LLMSetToolsFrame`) but does **not** re-connect MCP servers mid-call. Both of those frames are queued with `interruptible = False`, so a caller talking over the handoff lands before it or after it, never through the middle — the half-applied state (previous agent's prompt and tools, cleared context, new handlers underneath) raised nothing and logged nothing. The flag only covers the **drain**; an interruption also cancels a sync tool's task, and a frame that was never built cannot survive a drain. So the handoff is prepare-then-apply: `_prepare_handoff` does all of the I/O **and** builds both frames **before** `call_control.handoff_to_agent` commits `status=HANDED_OFF` + `active_agent_id`, and `_apply_handoff_context` then applies the switch with nothing left to wait on and nothing left that can raise (a raise after the commit is the same divergence with a deterministic trigger). `_complete_handoff` gates that on the switch actually committing — applying one the database refused is the same bug upside down — a DB round-trip *after* that commit left the call record handed off while the live pipeline kept the previous agent, logged nowhere, since `CancelledError` slips past `except Exception` (#144). Narrow by design: spoken frames are *supposed* to be dropped on barge-in. `test_handoff_survives_interruption.py` pins the drain half (both frames surviving, a spoken one still dropped, the flag not spreading); `test_handoff_switch_is_atomic.py` pins the cancellation half, and fails if an `await` reappears in the switch.

Tools run on voice **and** text, on every LLM provider — OpenAI-compatible (`tools`/`tool_calls`), Anthropic (`input_schema` + tool_use/tool_result blocks) and Bedrock Converse (`toolSpec`/`toolUse`/`toolResult`) each have their own dialect in `llm_text.py`. Built-ins are voice-only — all four resolve through `call_control` against a live `call_id`. Calls within one round run concurrently. Text turns cap at `_MAX_TOOL_ROUNDS` (5), then re-ask with the tools withheld so a reply always goes out.

### Tool Invocation Recording
All tool calls (webhook + MCP + builtin) are recorded in `tool_invocations` with input, output, status, latency_ms, and dispatched as `tool.result` events.

A row belongs to a **voice call** (`call_id`) or a **text session** (`session_id`) — a CHECK enforces exactly one. Read them back with `GET /v1/tools/invocations/{call_id}` or `GET /v1/chat/sessions/{session_id}/tool-invocations`.

Recording runs off the reply's critical path and is best-effort: a failure is logged, never raised, so losing the audit row can't cost the caller their answer.

## Pipecat Integration

All Pipecat imports isolated in `orchestrator/`. No other module imports Pipecat.

- `serializer.py` — Twilio mulaw ↔ PCM16 frames
- `pipeline_factory.py` — Builds pipeline from AgentConfig (providers, VAD, Smart Turn, voicemail)
- `transport_factory.py` — Creates a carrier media-stream, WebRTC, or WhatsApp transport. `CARRIER_MEDIA` is the one place a call's carrier picks a serializer and rate (see "Carriers")
- `call_session.py` — Per-call lifecycle, first message, cleanup
- `tool_bridge.py` — Registers tools + writes ToolInvocation records
- `observability.py` — Logs transcripts/events to DB + dispatches webhooks
- `turn_metrics.py` — one `metrics.turn` call event per turn (STT/LLM/TTS TTFB + user-to-bot latency, and `turn_end_ms` — the caller's silence to the turn's release, which Smart Turn or Flux decides; absent when unmeasured, #204), from pipecat's latency observer; stored, never dispatched, none in an eval (#176). Read them with `GET /v1/calls/{id}/events?event_type=metrics.turn`
- `delegation_events.py` — one `delegation.completed` call event per gpt-live-1 delegation (#247), `{mode, duration_ms, outcome}`, outcome `answered`\|`timed_out`\|`failed`\|`declined`. Stored, never dispatched, none in an eval — like `metrics.turn`. A request gpt-live-1 acknowledged with no event beside it is the one it never delegated. `timed_out` is client mode only (responses mode has no delegation timeout; #243 bounds each function call and the delegate answers past it); a client delegate that returned no text is `failed` — unless it had just ended the call, which is what it was asked, and reads `answered` (#262). `declined` (no delegate configured, e.g. no tools) is recorded with `mode: client`, the target gpt-live-1 asked for. Reads pipecat's private delegation run, pinned by `test_delegation_completed.py`. The same subclass arms the hangup gate for a client delegation that ended the call (#262, see the S2S row), and voices the idle check-in and reports a delegation in flight to the idle guard (#257)
- `telemetry.py` — OpenTelemetry tracing setup + Pipecat observers (latency/turn/LLM/transcription/startup). See ADR-0010
- `live_control.py` — the call's half of the control channel (#215): the request kinds a live call answers (`message`, `handoff`) and what each does to its pipeline; `CallSession.start()` holds the channel for the call's lifetime

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
`recording_status`, `recording_url`, `summary`, `analysis`, `agent_name`,
`agent_version`, `latency_summary`, `failovers` (`[{role, from, to, error}]`, `role` `stt`\|`llm`\|`tts`, #222/#223), and `transcript` (`[{role, text, timestamp}]`).

`latency_summary` is derived from the call's `metrics.turn` events (#179):
`{turns, user_to_bot, stages: {stt|llm|tts}, slowest_turn}`, each spread being
nearest-rank `p50_ms`/`p95_ms` plus `count`. Null with no measured turns (zeros
would read as "instant"); a stage no turn reported is absent. Never stored, and
the per-turn records never ride in the webhook.

`call.rejected` (#224) is an inbound call refused at the carrier over the
project's live-call limit: never answered, no call row, so `call_id` is null
and the payload carries `{reason, limit, transport, from_number, to_number,
provider_call_id}`. Dispatched only — there is no event log to store it on.

Stored-only call events — on the call's event log (`GET /v1/calls/{id}/events`),
never sent to webhook subscribers, none in an eval: `metrics.turn` (#176) and
`delegation.completed` (#247).

The call read (`GET /v1/calls[/{id}]`) carries the same derived fields —
`ended_reason`, `agent_name`, `agent_version`, `latency_summary`, `failovers` — from the same function,
`services/call_view.py`, so the read and the webhook cannot disagree (#175).
`ended_reason` is null on the read while the call is still live; agent fields
are null for an inline agent and for a deleted agent row.

`ended_reason` (derived, not stored): `customer_ended_call`, `assistant_ended_call`,
`customer_did_not_answer`, `customer_busy`, `customer_silent`, `voicemail`, `transferred`,
`max_duration_reached`, `pipeline_error`, `telephony_failed`, `unknown`.

`pipeline_error` is a call that **failed**, and the call's event log says why
in an `error.raised` event (`{stage, error}`). There are two ways in. A build
that raises: no `CallSession` exists, so `build_call_pipeline` fails the call
itself for every transport, and WebRTC answers `500 pipeline_start_failed`
instead of an SDP answer. (pipecat's handler logs a callback error and answers
anyway, which used to connect the browser to a peer with no pipeline behind
it.) Or a provider pipecat stops using mid-call, like a 401 or an unknown model:
pipecat then ends the pipeline *gracefully*, so without the event the call read
as `customer_ended_call`. Either way `call.ended` still goes out.

A provider with **no key at any level** is caught before the build
(`Credentials.missing_keys`, never in an eval since text mode opens no speech
provider). The event and the WebRTC 500 both carry `missing_keys: [account]`.
A key that is present but refused mid-call (pipecat's `AUTHENTICATION` /
`AUTHORIZATION`) carries `provider` and `key_rejected: true`, so a console can
name the key without parsing the vendor's sentence.

Since pipecat 1.12 a user turn that VAD opened and closed with **no
transcript** — a cough, a door, an unrecognized word — is handled rather than
dropped, and it is on by default. One that *interrupted* the agent gets a
single LLM run so the agent asks the caller to repeat instead of stopping dead
mid-sentence; one that arrives while the agent was already waiting is left
unanswered, because replying to a passing truck is intrusive and the idle guard
below already covers a caller who has genuinely gone quiet. See CONTEXT.md,
"empty user turn" — it is a turn *outcome*, not a fourth timeout.

`customer_silent` comes from `user_idle_timeout_ms` (default `10000`, `0`
disables): after the agent stops speaking, a caller who stays quiet that long
hears `idle_message` ("Are you still there?"), and a second consecutive silence
ends the call. Speaking again resets the count. Cascade speaks the line through
TTS; S2S has no TTS stage, so the model is asked to check in and words it
itself — gpt-live-1 as spoken commentary, since it reads no context after its
first (#257). The timer restarts only when the agent stops speaking, so a
check-in the model never voices leaves the line silent with no second strike,
until `max_call_duration_seconds`. A silence while gpt-live-1's delegate is
still working is not a strike: the caller is waiting on an answer, and pipecat
holds the timer only for a function call in the call's own pipeline. Like
`max_duration_reached` it records a marker event (`call.customer_silent`) and is inferred **above** `assistant_ended` — ending
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

### Committing

`git commit -s`. CI's `dco` job fails a pull request carrying any commit
without a `Signed-off-by:` trailer, and `Co-Authored-By:` does not satisfy it.
`.claude/rules/common/git-workflow.md` has the recovery commands.

### Domain docs

Single-context: `CONTEXT.md` and `adr/` at the repo root — not `docs/adr/`, which
is where the sibling builder repo puts its ADRs.
