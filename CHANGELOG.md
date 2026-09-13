# Changelog

Notable changes to TurnCall. Format based on [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[semantic versioning](https://semver.org/): from 1.0.0 on, a breaking change to
the REST API, the agent config schema, or the webhook payloads means a major
version bump. Breaking changes are always listed here.

History begins at 1.0.0. Earlier development predates the first public release
and is not part of this repository's history.

## [Unreleased]

### Added

- **Tool calling on SMS, chat and WhatsApp text.** Text conversations could not
  call tools at all — not MCP, not the agent's own webhook tools. An agent that
  books meetings on a phone call would claim it had booked one over SMS, or
  apologise; either way the webhook was never hit. Every provider family has its
  own dialect: OpenAI-compatible (`tools`/`tool_calls`), Anthropic
  (`input_schema` + tool_use/tool_result blocks) and Bedrock Converse
  (`toolSpec`/`toolUse`/`toolResult`). Calls within one round run concurrently.
  The loop caps at five rounds and then re-asks with the tools withheld, so a
  model that keeps calling them still answers in words. Built-ins stay voice-only
  — all four resolve against a live `call_id`.
- **Text-session tool calls are recorded.** `tool_invocations.call_id` is
  nullable, `session_id` joins it, and a CHECK enforces exactly one. Read them
  back at `GET /v1/chat/sessions/{id}/tool-invocations`. `output_json` and
  `latency_ms` were columns nothing wrote; both paths now time the call and
  classify the result through one helper, so a tool that returned
  `{"error": ...}` is recorded as failed on voice as it already was on text.
- **`execution_mode: "async"` does something.** Accepted by the API schema since
  tools existed and read nowhere. `async` lets a slow tool outlive an
  interruption and delivers its result when it arrives; `sync` stays the default,
  which matters most for the built-ins — `end_call` must not hang up after the
  caller said never mind. Voice only, and now documented as such.
- **`max_call_duration_seconds` caps a call.** Validated 60–14400 and enforced
  never, so a stuck call ran until the carrier stopped it, billed the whole way.
  A watchdog on the session cancels the worker, and the call finalizes the way a
  hangup does — with its own `ended_reason`, `max_duration_reached`, rather than
  reading back as `customer_ended_call`.
- **`interruption_enabled: false` turns off barge-in.** Cascade only. On S2S the
  realtime service owns turn-taking, so it is warned about rather than
  half-applied.
- **`silence_timeout_ms` sets the VAD stop window**, and
  `PIPECAT_VAD_CONFIDENCE_THRESHOLD` is honoured. Both were documented and inert.
- **`MCP_MAX_TOOLS_TOTAL`** (default 100) — `MCP_MAX_TOOLS_PER_SERVER` is per
  server and does not compose; ten servers at the default would advertise 500
  tools. **`MCP_CONNECT_TIMEOUT_SECONDS`** (default 10) bounds discovery: past it
  the call proceeds with no MCP tools rather than not proceeding, because losing
  the tools degrades the agent and losing the call is an outage.
  **`TOOL_MAX_RESPONSE_BYTES`** caps a custom webhook tool's result, as
  `MCP_MAX_RESPONSE_BYTES` already did for MCP.
- **A `live` test marker** for the paths a hermetic suite cannot reach, excluded
  from `make test` and skipped per missing credential. It exists because the
  `openai` 3 upgrade moved the HTTP stack under every OpenAI service, and the
  `mcp` 2 rename emptied every MCP server, without a test going red either time.

### Changed

- **Pipecat 1.10 and the `openai` 3 SDK.**
- **Both MCP SDK lines are supported** (`mcp>=1.27,<3`) and the `<2` pin is
  gone. 2.x renamed `Tool.inputSchema` → `input_schema` and
  `CallToolResult.isError` → `is_error`; the client reads whichever spelling is
  present and builds its own HTTP client from whichever httpx family the SDK was
  built against. Live tests cover all three transports on both lines.
- **Claude is sent no `temperature`**, on the direct Anthropic path and through
  Bedrock. Current models answer `400 "temperature is deprecated for this
  model."`, which made the service unusable and ended the call on its first LLM
  turn. The deprecation belongs to the model, so it travels with the model rather
  than the endpoint — the other vendors Bedrock fronts still get one. Set one
  deliberately on an older model with `llm.extra`.
- **MCP name precedence follows config order.** Discovery moved into
  `asyncio.gather` for one round trip instead of three, and name claiming went
  with it — so "first server wins" had become "fastest server wins", and which
  server owned a contested name could differ call to call with no config change.
  Fetch and claim are now separate: the handshakes still overlap, the claim is a
  serial pass in the agent's configured order.
- **`ruff format` runs in CI** and the configured line length is 88, which is
  what the code was already wrapped at. `line-length = 100` never matched, so
  anyone with format-on-save got a diff full of lines they never touched. The 79
  reformatted files were verified AST-identical, not merely test-passing.

### Fixed

- **MCP servers now connect on WebRTC and WhatsApp voice.** `mcp_servers` was
  read only by the Twilio path, so an agent with MCP tools got none of them on
  those transports — the model saw an empty tool list.
- **S2S advertised none of its MCP tools.** It connected the servers, paid the
  handshake and held the sessions for the whole call; only the schema was
  missing.
- **A tool name claimed by two MCP servers** put two identical function names in
  one request, which providers reject, and the loser was advertised but
  unreachable. Precedence is built-in > agent tools > MCP, the loser skipped and
  logged. An MCP server can no longer claim a built-in's name — a discovered tool
  called `end_call` was advertised and then hung the call up.
- **The tool that ran was not always the tool the model was shown.** The
  advertised schema gave a contested name to the agent's own tool; voice dispatch
  asked the MCP manager first. The model saw the customer's description and
  parameter schema, and the MCP server's tool ran.
- **`handoff_to_agent` moved the prompt and left the tools.** The model believed
  it was the new agent while holding the previous one's. It now replaces the
  advertised set; MCP servers are not re-connected mid-call, which is logged.
- **An unbounded MCP tool result** put megabytes into the context, where it is
  charged on every later turn until the call dies on context length. Over the cap
  the model gets an error plus a 512-byte preview.
- **Discovered tool schemas lost their `$defs`.** A property holding a `$ref`
  arrived pointing at nothing, and providers reject an unresolvable `$ref` — so
  the tool failed at the model rather than here.
- **`{"error": null}` was recorded as a failure**, so an endpoint that always
  includes the field had every successful call logged as failed.
- **A cancelled pipeline build leaked its open MCP sessions** — cleanup sat after
  the `try` and an `except Exception` walked past it.
- **`llm.extra` reaches Anthropic.** The documented way to set a temperature
  crashed on the first LLM turn: the SDK dropped `temperature`, `top_k` and
  `top_p` from `messages.create()`. Any key the SDK will not take by name now
  travels in `extra_body`, checked against the SDK's own signature.
- **An inline agent from call-init crashed every Twilio call**, and then got no
  post-call processing on any transport — and since that trigger dispatches
  `call.ended`, such a call completed and told nobody. WebRTC additionally
  resolved its inline config and discarded it, so every browser call with an
  inline agent finalized silently. All three transports now share one sentinel
  and one rule about recording the config. See `adr/0017`.
- **`AgentConfig.knowledge_bases` and `ToolDefinition.is_builtin` are gone.**
  Neither could ever be set or read. A `config_blob` written while the field
  existed still loads.

### Security

- **The BYOM/MCP URL allowlist could be talked past.** `fnmatch`'s `*` spans
  `/`, so a pattern an operator would reasonably write —
  `https://*.trusted.com/*` — also accepted `https://evil.com/x.trusted.com/y`,
  `https://evil.com/?u=https://api.trusted.com/` and a `user@` prefix. Each
  leaves for `evil.com` from inside the network, which is the exact thing the
  gate exists to stop. A pattern naming a host must now match the URL's host on
  its own. Patterns naming no host (`*`, `*openai*`) are unchanged.
- **MCP urls now go through the same SSRF gate** as `llm.base_url` and
  `s2s.base_url`. They had none.

### Documentation

- **`adr/0017` — calls without an agent row.** Why the zero-UUID sentinel
  exists, why it is never written to `active_agent_id`, and what a null
  `agent_id` on an event does and does not mean.
- **A config-reachability guard** (`tests/unit/`) walks the agent-config models
  and asserts each field is read by something outside `domain/` and `api/`. The
  most common defect here is a field that validates, stores, round-trips through
  the API and does nothing; eight had landed in two days. A new field must now be
  consumed or explicitly declared inert.
- `voicemail_expected_duration_seconds` remains inert, with the reason recorded:
  Pipecat's `VoicemailDetector` has nothing that means "expected greeting
  length", and mapping it onto `voicemail_response_delay` would put a 15-second
  pause after the greeting. It needs behaviour or removal on the next major.

## [1.1.0]

### Added

- **AWS Bedrock as an LLM provider and Amazon Nova Sonic 2 as S2S.** Model ids
  pass through verbatim, so direct ids, cross-region inference profiles and
  provisioned-throughput ARNs all work. Credentials come from a per-agent `aws`
  block — an assumed `role_arn`, static keys, a named profile, or the ambient
  chain. See `adr/0016`.
- **`openai_live` S2S provider** — OpenAI's `gpt-live-1`, which is full duplex:
  it listens and speaks at once and handles being talked over itself, where the
  Realtime API is turn-based. Reasoning and tool calls can be delegated to a
  backend text model with `s2s.extra.backend_model`. `s2s.temperature` is
  accepted here and `s2s.turn_detection` must stay `server_vad`.
- **`stt.extra` and `tts.extra` now reach the provider.** Both fields existed
  and were silently ignored. On Deepgram, `stt.extra` unlocks
  `profanity_filter`, `diarize`, `redact`, `keyterm`, `version` and
  `utterance_end_ms`.
- **`tts.speed` now works on Deepgram, OpenAI and ElevenLabs**, not Cartesia
  alone. `1.0` still sends nothing.

### Changed

- **Upgraded to Pipecat 1.9.0** from 1.8.1.
- **Provider defaults refreshed.** These apply only to agents that do *not* set
  the value explicitly:

  | Role | Was | Now |
  |---|---|---|
  | Deepgram STT | profanity filter on | off |
  | OpenAI STT | `gpt-4o-transcribe` | `gpt-transcribe` |
  | Cartesia TTS | `sonic-3.5` | `sonic-3.6` |
  | HeyGen avatar | VP8 | H264 |

  `gpt-4o-transcribe` is withdrawn on 2027-02-26 and LiveAvatar has deprecated
  VP8. **Deepgram transcripts are no longer profanity-filtered**: the filter
  rewrites matched words rather than tagging them, so one false positive
  silently altered a transcript you store, analyse and receive in `call.ended`.
  Set `stt.extra.profanity_filter` to `true` to restore it.
- **Retrieved knowledge is attached as `developer`, not a second `system`
  message.** It is guidance for one turn, not the agent's instructions, and
  outside OpenAI a mid-conversation `system` message was downgraded to `user`
  anyway.
- **The system prompt is set on the LLM service rather than inserted as the
  first context message.** Internal; no config or API difference. The
  conversation context now starts empty, so a transcript no longer carries the
  instructions as a phantom first turn.

### Fixed

- **A Deepgram voice name was being sent to the other TTS providers.**
  `tts.model` and `tts.voice` both defaulted to `aura-2-helena-en` whatever the
  provider, so an agent choosing Cartesia, OpenAI or ElevenLabs without naming
  a model and voice was misconfigured. Each provider now falls back to its own.
- **`end_call` during voicemail detection.** While the classifier's gate was
  closed, the frame ending a call could be dropped instead of reaching the
  pipeline, leaving the caller on the line until the idle timeout.
- **Assistant transcripts no longer contain text the model never wrote.** A
  word-timing event matching nothing left to speak was added to the
  conversation as spoken text.
- Replaced end-of-life Bedrock model ids and documented inference profiles.
- Stopped the localstack compose overriding real AWS credentials.

### Upgrade notes

- **Config that was silently ignored now takes effect.** If an agent carries a
  stray `stt.extra`, `tts.extra` or `tts.speed`, it did nothing before this
  release and does something now. Worth a scan before upgrading.
- No REST API, agent config schema or webhook payload shape changed, which is
  why this is a minor release. The `call.ended` transcript *content* changes
  with the profanity filter default.

## [1.0.0]

First public release.

### Changed

- **Upgraded to Pipecat 1.8.1** from 1.5.0.
- **Provider model defaults refreshed.** These apply to agents that do *not*
  set a model explicitly. Pin the old value in your agent config to keep it:

  | Role | Was | Now |
  |---|---|---|
  | ElevenLabs TTS | `eleven_turbo_v2_5` (deprecated upstream) | `eleven_flash_v2_5` |
  | Cartesia TTS | `sonic-3` | `sonic-3.5` |
  | Deepgram STT | `nova-2` | `nova-3-general` |
  | OpenAI Realtime | `gpt-4o-realtime-preview` | `gpt-realtime-2.1` |
  | Realtime transcription | `gpt-4o-transcribe` | `gpt-realtime-whisper` |

- **A pipeline now ends when a service can no longer work.** Pipecat 1.8 stops
  using an STT/TTS/LLM after an unrecoverable failure (rejected API key, unknown
  model or voice, a connection that won't re-establish). TurnCall sets
  `processor_unusable_policy=END` so the call terminates and finalizes promptly,
  rather than leaving the caller on a silent line until the idle timeout.

### Fixed

- **Per-agent Deepgram STT config was ignored.** `stt.model` and `stt.language`
  were hardcoded to `nova-2` and `en` for the Deepgram provider, discarding
  whatever an agent specified. Both are now honored. If you have non-English
  agents on Deepgram, their configured language now actually takes effect —
  verify transcription quality after upgrading.

### Security

- Widened `.gitignore` env patterns to `.env*` and `*.env`. The previous rules
  (`.env`, `.env.*`) did not match variants such as `.env2`.

### Removed

- `pyyaml-include` (GPL-3.0) is no longer in the dependency tree; Pipecat
  dropped it in 1.6.0. TurnCall's dependencies are now fully permissive.

### Added

- CI: lint, test (with pgvector + migrations), and `bandit` on every push and PR.
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and this changelog.
- `webrtc-video` extra, required for video avatars over SmallWebRTC once
  Pipecat 2.0 drops OpenCV from the `webrtc` extra.
