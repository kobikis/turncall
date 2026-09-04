# Changelog

Notable changes to TurnCall. Format based on [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[semantic versioning](https://semver.org/): from 1.0.0 on, a breaking change to
the REST API, the agent config schema, or the webhook payloads means a major
version bump. Breaking changes are always listed here.

History begins at 1.0.0. Earlier development predates the first public release
and is not part of this repository's history.

## [Unreleased]

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
