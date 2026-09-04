# HeyGen video avatar: WebRTC + cascade only, on SmallWebRTC

Added an optional HeyGen live video avatar to voice agents. `HeyGenVideoService`
sits between `tts` and `transport.output()`, consuming TTS audio and emitting
avatar video frames. Opt-in per agent via a minimal `AvatarConfig`
(`enabled`/`provider`/`avatar_id`/`is_sandbox`), mirroring `VoicemailConfig`.

## Scope decisions (the explicit no's)

- **WebRTC only.** An avatar needs a video channel back to the user. Twilio PSTN
  and WhatsApp voice are audio-only, so the avatar is gated to the WebRTC
  browser transport. The `avatar_enabled` flag is passed only by the WebRTC
  caller; phone paths never see it.
- **Cascade only.** The avatar taps the `tts` stage. S2S has no `tts` (the
  realtime model emits audio directly), so wiring it there is a different,
  unverified path. Avatar-on-S2S is skipped with a warning, not built.
- **SmallWebRTC, not Daily.** SmallWebRTC already sends live video out (just
  enable `video_out_*` on the existing transport), so we avoid adding Daily as a
  paid dependency + new transport. Daily would only earn its place if we later
  need server-side recording or multi-party calls.

## Update: Tavus as a second provider

`AvatarConfig.provider` now accepts `heygen` | `tavus` (`_create_avatar_service()`
in `pipeline_factory`). Tavus is the higher-quality / lower-latency option
(sub-600ms, 1080p). Note the `tavus` extra pulls `daily-python` — this does **not**
contradict the "no Daily" decision above: Daily is Tavus's *internal* leg to its
avatar servers (like HeyGen's internal LiveKit), and the avatar video still comes
back as frames into our pipeline. The **user-facing** transport stays SmallWebRTC.
Tavus needs `TAVUS_API_KEY` + a `replica_id`; `persona_id` defaults to
`pipecat-stream` (lip-syncs Pipecat TTS).

## Known ceiling

The HeyGen `aiohttp.ClientSession` is created in `pipeline_factory` and not
closed there (no teardown hook) — one leaked session per avatar call, matching
the existing ElevenLabs STT pattern. Close it on call teardown if it ever
matters.
