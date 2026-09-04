# Pipecat Features in TurnCall

What TurnCall uses from Pipecat (1.4), what it doesn't, and what each thing buys you.

**Legend:** ✅ used · ⚠️ custom/partial (TurnCall rolls its own instead of Pipecat's) · ❌ not used
**"Where"** points at the TurnCall code, all under `src/turncall/orchestrator/` unless noted.

---

## Transports & Telephony

| Feature | Using? | Value | Where |
|---|---|---|---|
| Twilio WebSocket Media Streams | ✅ | PSTN phone calls over a WS audio stream (μ-law 8kHz) | `serializer.py`, `transport_factory.py`, `webhooks/media_stream.py` |
| Small WebRTC (peer-to-peer) | ✅ | Browser/in-app voice with no SFU/vendor cost | `transport_factory.py` |
| WhatsApp Business Calling | ✅ | Inbound WhatsApp voice via WebRTC | `transport_factory.py`, `webhooks/whatsapp_*` |
| FastAPI WebSocket transport | ✅ | The WS plumbing under Twilio media streams | (implicit in media_stream) |
| Daily / LiveKit WebRTC | ❌ | Managed SFU for group calls, recording, scale-out. Worth it only if you outgrow peer-to-peer WebRTC | — |
| Avatar transports (Tavus, HeyGen, Bey, LemonSlice) | ❌ | Adds a talking-head video layer to the agent | — |
| Other telephony serializers (Telnyx, Plivo, Exotel, Vonage, Genesys, Asterisk SIP) | ❌ | Same media-stream pattern for non-Twilio carriers / contact-center stacks | — |

---

## Speech-to-Text (STT)

| Feature | Using? | Value | Where |
|---|---|---|---|
| Deepgram (Nova-2, streaming) | ✅ default | Low-latency streaming transcription | `pipeline_factory.py:_create_stt_service` |
| OpenAI / ElevenLabs / Cartesia Ink | ✅ optional | Per-agent provider choice | same |
| `supports_ttfs` flag (1.3) | ❌ | Suppresses false latency warnings on turn-based STT — only matters if you adopt a turn-based STT | — |
| AssemblyAI context carryover, Soniox endpoint sensitivity, Azure profanity (1.4) | ❌ | Provider-specific accuracy knobs | — |
| 15+ other STT vendors | ❌ | Vendor lock-in / pricing / language coverage alternatives | — |

---

## Large Language Models (LLM)

| Feature | Using? | Value | Where |
|---|---|---|---|
| OpenAI (chat completions) | ✅ default | Main reasoning model | `pipeline_factory.py:_create_llm_service` |
| Anthropic Claude | ✅ | Alt frontier model | same |
| Ollama (local) | ✅ | Self-hosted / zero-API-cost models | same |
| `custom_openai` (any OpenAI-compatible URL) | ✅ | BYOM endpoints, allowlisted via `BYOMSettings` | same + `_validate_byom_url` |
| Universal `LLMContext` / `LLMContextAggregatorPair` | ✅ | Provider-agnostic context + aggregation (the 1.0 universal API) | `pipeline_factory.py` |
| `append_system_instruction` (1.3) | ❌ | Durable system text that survives context resets — relevant to your handoff path, which resets context | — |
| Direct-function tool auto-registration (1.4) | ❌ (by choice) | Collapses schema + handler registration; you keep the two-phase split on purpose | ADR `0001` |
| AWS Bedrock, Gemini, Grok, Mistral, Fireworks, OpenRouter, Perplexity, … | ❌ | More model/hosting options | — |

---

## Text-to-Speech (TTS)

| Feature | Using? | Value | Where |
|---|---|---|---|
| Deepgram Aura-2 | ✅ default | Low-latency voice | `pipeline_factory.py:_create_tts_service` |
| OpenAI / ElevenLabs / Cartesia Sonic-3.5 | ✅ optional | Voice quality / emotion / per-agent choice | same |
| Cartesia `sonic-3.5` default (1.2) | ✅ | Adopted in the 1.8.1 upgrade | `pipeline_factory.py:_create_tts_service` |
| Word-level timestamps / `AggregatedTextProgressFrame` (1.4) | ❌ | Per-word highlighting for UIs; useless for phone-only | — |
| TTS caching | ❌ | Cache repeated utterances (greetings, IVR prompts) to cut latency+cost | — |
| `MarkdownTextFilter` | ❌ | Strips Markdown so the model's `**bold**` isn't spoken aloud | — |
| 20+ other TTS vendors | ❌ | Voice/price/language alternatives | — |

---

## Speech-to-Speech (Realtime / S2S)

| Feature | Using? | Value | Where |
|---|---|---|---|
| OpenAI Realtime API | ✅ | Native audio-in/out, lowest latency | `s2s_config.py:_create_openai_realtime` |
| Google Gemini Live | ✅ | Alt native-audio model | `s2s_config.py:_create_gemini_live` |
| `realtime_service_mode=True` (1.3/1.4) | ✅ | Correct trailing context-write semantics + auto-swapped turn strategies for realtime services | `pipeline_factory.py:_create_s2s_pipeline` |
| Server-VAD vs `pipecat_vad` turn modes | ✅ | Choose service-side VAD or local Silero | `s2s_config.py`, `pipeline_factory.py` |
| `user_audio_preroll_secs` (1.4) | ❌ | Replays leading audio when locally-driven turns start; tune if you hear clipped first words | — |
| AWS Nova Sonic, Grok Realtime, Ultravox, Inworld Realtime | ❌ | More S2S providers | — |

---

## VAD & Turn Detection

| Feature | Using? | Value | Where |
|---|---|---|---|
| Silero VAD (local ONNX) | ✅ | Detects speech for barge-in / interruption | `pipeline_factory.py` (on user aggregator) |
| Smart Turn v3 (local, vendored numpy) | ✅ | ML end-of-turn detection; 1.3 cut its RAM ~566→60MB & cold-start ~5s→0.3s (free on the bump) | `pipeline_factory.py` `LocalSmartTurnAnalyzerV3` |
| `TurnAnalyzerUserTurnStopStrategy` / `UserTurnStrategies` | ✅ | Wires Smart Turn into the user aggregator | same |
| `FilterIncompleteUserTurnStrategies` / `deferred()` (1.2) | ❌ | LLM-gated "did the user actually finish?" — fewer mid-sentence interruptions | — |
| Krisp VIVA / TEN / FIRE RED VAD | ❌ | Noise-robust or streaming VAD alternatives | — |
| Krisp VIVA turn detection | ❌ | Commercial turn model alternative to Smart Turn | — |

---

## Audio Processing

| Feature | Using? | Value | Where |
|---|---|---|---|
| Custom resampler (8/16k ↔ 24k for S2S) | ⚠️ custom | Bridges Twilio 8kHz to S2S 24kHz | `audio_resampler.py` |
| Noise filters (Krisp, RNNoise, Koala, AIC) | ❌ | Suppress background noise before STT — big quality win on noisy PSTN calls | — |
| `SoundfileMixer` | ❌ | Mix in hold music / background audio | — |
| `AudioBufferProcessor` | ❌ | Buffer frames for call recording | — |

---

## Tools, Function Calling & MCP

| Feature | Using? | Value | Where |
|---|---|---|---|
| Function calling (register handlers on LLM) | ⚠️ custom bridge | TurnCall's tool layer (builtin + webhook tools) over Pipecat's `register_function` | `tool_bridge.py` |
| Builtin tools (`end_call`, `transfer_call`, `handoff_to_agent`, `send_dtmf`) | ✅ | Call-control verbs the LLM can invoke | `tool_bridge.py` |
| MCP client | ✅ | Auto-discover external tools at call start via `tools/list` | `services/mcp_client.py`, `tool_bridge.py` |
| DTMF aggregation (inbound keypad) | ❌ | Capture caller keypresses (e.g. "press 1") as input — you only *send* DTMF, don't receive | — |
| IVR navigation | ❌ | Have the agent navigate *other* phone menus on outbound calls | — |
| `@tool_options(cancel_on_interruption=…)` (1.4) | ❌ | Per-tool interruption/timeout control | — |

---

## Knowledge & Memory

| Feature | Using? | Value | Where |
|---|---|---|---|
| RAG / knowledge retrieval | ⚠️ custom | TurnCall's own pgvector RAG (prompt/auto/tool modes) instead of Pipecat's Moss | `orchestrator/knowledge_processor.py`, `services/retrieval.py` |
| Mem0 / Synap (long-term memory) | ❌ | Cross-session memory ("remember me from last call"). Note: Mem0 2.0 API changed in 1.4 | — |
| Context summarization (auto token mgmt) | ❌ | Auto-summarize long conversations to stay under token limits — matters for very long calls | — |

---

## Conversation Flows (Pipecat Flows)

| Feature | Using? | Value | Where |
|---|---|---|---|
| Pipecat Flows (structured nodes/transitions) | ❌ | Deterministic, node-based conversation graphs with per-node tools/prompts/actions. The biggest thing you're *not* using — replaces free-form prompting for scripted flows (intake, qualification, booking). Separate `pipecat-ai-flows` package | — |

---

## Voicemail & Special Cases

| Feature | Using? | Value | Where |
|---|---|---|---|
| `VoicemailDetector` + retry backoff | ✅ | Detect answering machines on outbound, leave a message | `pipeline_factory.py:431` |
| `TTSSpeakFrame(append_to_context=…)` (1.4 default flip) | ✅ | Direct TTS playback; you pin `False` so prompts don't leak into context | `:493`, `call_session.py:92` |
| User idle detection | ❌ | Detect a silent caller and prompt/hang up ("Are you still there?") | — |

---

## Observability, Metrics & RTVI

| Feature | Using? | Value | Where |
|---|---|---|---|
| Transcript/event taps | ⚠️ custom | Frame-tap processors → DB + webhooks (cascade only) | `observability.py` |
| OpenTelemetry tracing | ❌ | Distributed traces of pipeline latency (STT/LLM/TTS spans) — real win for debugging slow calls | — |
| Built-in observers (latency, LLM, turn, transcription, startup-timing) | ❌ | Drop-in logging of TTFB, turn timing, startup cost without writing tap processors | — |
| Sentry / Datadog / Roark / Finchvox / Arize | ❌ | Hosted error & call analytics | — |
| RTVI protocol + `RTVIObserver` | ❌ | Standard wire protocol for browser/mobile clients (events, transcripts, VAD state) | — |
| Client SDKs (JS, React, iOS, Android, RN, C++) | ❌ | Prebuilt client libs — you expose your own REST API instead | — |
| **Gap:** S2S transcript logging | ❌ | The transcript taps aren't in `_create_s2s_pipeline`, so S2S calls log no transcripts. Pre-existing | `pipeline_factory.py:719` |

---

## Agent Architecture (1.3+)

| Feature | Using? | Value | Where |
|---|---|---|---|
| Workers / multi-agent (`BaseWorker`, `LLMWorker`, `WorkerRunner`) | ❌ | Multiple coordinating agents on a shared bus, job dispatch, handoff/activation. You do handoff via a custom tool + context reset instead | `tool_bridge.py:handoff` |
| `UIWorker` (drive a web UI over RTVI) | ❌ | LLM that reads/controls a browser page — not relevant to phone | — |
| Distributed/proxy agents | ❌ | Inter-process agent messaging across buses | — |

---

## Evals, CLI & Deployment

| Feature | Using? | Value | Where |
|---|---|---|---|
| `pipecat.evals` framework (1.4) | ❌ | YAML scenario tests with scripted convos, latency budgets, LLM-judged criteria (text/audio). Real value for regression-testing agents | — |
| `pipecat init` / `create` scaffolding | ❌ | Generates agent projects + `CLAUDE.md`/`AGENTS.md` | — |
| Pipecat Cloud (managed hosting, REST API, warm pools) | ❌ | Managed deployment/scaling — you self-host via Docker | `Dockerfile`, `docker-compose.yml` |
| WebSocket HMAC auth / origin restriction (1.4) | ❌ (by choice) | One-time WS tokens + CSWSH protection — you sit behind Twilio's auth | ADR `0001` |
| HIPAA compliance framework | ❌ | If you ever handle PHI | — |

---

## Pipeline Utilities & Frames

| Feature | Using? | Value | Where |
|---|---|---|---|
| `Pipeline` / `PipelineTask` / `PipelineRunner` | ✅ | Core pipeline lifecycle | `call_session.py` |
| Frame types (Transcription, Text, LLM, Control, System) | ✅ | The data flowing through processors | throughout |
| `ParallelPipeline` (multi-branch) | ❌ | Run branches concurrently (e.g. transcribe + classify in parallel) | — |
| Heartbeat frames / idle detection | ❌ | Pipeline health monitoring | — |
| `PatternPairAggregator` | ❌ | Stream-parse tagged spans (e.g. `<emotion>…</emotion>`) out of LLM output | — |

---

## Highest-value things you're NOT using (shortlist)

1. **Noise suppression filters** (Krisp/RNNoise) — biggest call-quality lever on noisy PSTN audio.
2. **Pipecat Flows** — if any agent is a scripted flow (intake/booking/qualification), this replaces brittle prompt engineering.
3. **OpenTelemetry + latency observers** — turn "the call felt slow" into per-stage spans.
4. **`pipecat.evals`** — regression-test agents before shipping prompt/model changes.
5. **Inbound DTMF aggregation** — you send DTMF but can't receive keypresses; blocks "press 1 for sales" style flows.
6. **Fix the S2S transcript-logging gap** — not a Pipecat feature, just wire the existing taps into the S2S pipeline.
