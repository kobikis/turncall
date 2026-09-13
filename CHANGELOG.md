# Changelog

Notable changes to TurnCall. Format based on [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[semantic versioning](https://semver.org/): from 1.0.0 on, a breaking change to
the REST API, the agent config schema, or the webhook payloads means a major
version bump. Breaking changes are always listed here.

History begins at 1.0.0. Earlier development predates the first public release
and is not part of this repository's history.

## [Unreleased]

### Fixed

- **A WebRTC call running an inline agent now produces a `call.ended`.** An
  agent that arrives whole in a call-init response has no row in `agents`, so
  everything after the hangup reads the config off the call itself. Twilio and
  WhatsApp recorded it there; WebRTC resolved it, ran on it, and discarded it —
  so every browser call with an inline agent finalized with the right status,
  duration and transcript, and then went quiet: no post-call analysis, no
  `call.ended` webhook, and nothing downstream of it. A structural test now
  holds all three transports to the same two rules. See `adr/0017`.

### Documentation

- **`adr/0017` — calls without an agent row.** Why the zero-UUID sentinel
  exists, why it is never written to `active_agent_id`, and what a null
  `agent_id` on an event does and does not mean.

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
