# HeyGen video avatar: WebRTC only, on SmallWebRTC

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
  *Reversed by #401, below.*
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

## Update: an Avatar never ends the call (#402)

An Avatar that cannot show — the call is not a browser call, the id is
missing, the provider refused the session or it stopped mid-call —
leaves the call running as voice, and the call's event log says why in a
stored-only `avatar.unavailable` `{provider, reason}`. The transport gate
above now records its reason instead of only logging it
(`orchestrator/avatar.avatar_gate`, given `carries_video` by the WebRTC entry
point alone). pipecat's services treat a refused or dropped session as a log
line and keep consuming the TTS audio, so the caller heard nothing; TurnCall
subclasses both to watch the Avatar (a session at setup, its video arriving,
the Avatar leaving its room) and pass the spoken frames on once it is gone
(`orchestrator/avatar_watch.py`).

## Update: speech-to-speech too (#401)

The "cascade only" no above was about the `tts` stage, but the services never
needed the stage, only the agent's audio: they read `TTSAudioRawFrame`, and
OpenAI Realtime, Gemini Live, Nova Sonic and Grok Voice all speak it
themselves. So on S2S the Avatar sits after the realtime service (after its
output resampler; both providers resample to their own rate) and before
`SpeechBeforeAmbience` and `transport.output()`, where it sits after TTS in
cascade. So the build skips no S2S Avatar, and records no reason for one.

gpt-live-1 (`openai_live`) is the one service that does not: it streams
`SpeechOutputAudioRawFrame` continuously, silence included, at real-time pace.
Behind an Avatar that would be no lip-sync and two real-time streams into one
output (gpt-live-1's and the Avatar leg's). So on S2S the Avatar reads through
`SpeechForTheAvatar` (`orchestrator/avatar_speech.py`), which turns a streamed
voice into replies: speech and the pauses inside it (up to pipecat's
`BOT_VAD_STOP_SECS`) as TTS audio, a `TTSStoppedFrame` after a longer silence
(Tavus's end-of-utterance mark), the silence between replies dropped, a
barge-in ending the reply. TTS audio passes untouched, so it is in the
pipeline for every provider rather than chosen per provider. With the Avatar
fallen back to voice, the caller hears those TTS frames as on cascade.

Barge-in is the call's: an Avatar cuts its reply at an `InterruptionFrame`,
which the realtime services pass downstream. pipecat's Tavus already did;
pipecat's HeyGen cut at `UserStartedSpeakingFrame` instead, so
`WatchedHeyGenVideoService` moves its cut to the interruption. gpt-live-1 is
why: it broadcasts no interruption, it hears the caller and decides itself
whether to stop, and on a caller it talked through HeyGen's Avatar fell silent
mid-reply (as it did on a cascade call with barge-in off). When gpt-live-1
does stop for the caller it only goes quiet — its API has no truncation event
— so the Avatar plays out what it had already taken, up to a second past
gpt-live-1 on HeyGen. Verified by unit tests (placement per provider, the
stream conversion, where each Avatar cuts); the live check against a sandbox
Avatar is still to do.

## Known ceiling

The HeyGen `aiohttp.ClientSession` is created in `pipeline_factory` and not
closed there (no teardown hook) — one leaked session per avatar call, matching
the existing ElevenLabs STT pattern. Close it on call teardown if it ever
matters.
