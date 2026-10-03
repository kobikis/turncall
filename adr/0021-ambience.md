# ADR-0021: Ambience — a room the agent sounds like it is in

**Status:** Accepted
**Date:** 2026-09-30
**Relates to:** ADR-0004 (call audio QoS), ADR-0005 (app-side recording),
ADR-0011 (project deletion), ADR-0017 (calls without an agent row),
ADR-0018 (eval transport).

## Context

An agent that answers in a perfectly silent room sounds like software. Every
comparable platform ships some form of background audio for this, and pipecat
already has the mechanism: `TransportParams.audio_out_mixer` takes a
`BaseAudioMixer`, and `SoundfileMixer` mixes a looping file into whatever the
transport sends.

Almost everything interesting about this feature comes from four properties of
that mixer and of TurnCall's own audio path, all measured rather than assumed.

**1. A mixer makes the output continuous.** With one attached, the output
transport stops waiting on its audio queue: when the queue is empty it
synthesizes a mixer-only frame from silence and sends it, for the whole call.
So ambience plays under everything and between everything, and the transport
emits ~50 frames/sec/call for the call's entire duration rather than only while
the agent speaks. That is a change to the path ADR-0004 was written about, so it
was measured before being believed:

| per 20 ms frame | median | per call |
|---|---|---|
| `SoundfileMixer.mix()` @ 8k / 16k | 4.8 µs / 5.2 µs | 0.024% of a core |
| TurnCall's Twilio serializer | 2.8 µs | 0.014% of a core |
| **combined** | **7.6 µs** | **0.038% of a core** — ~2600 calls/core |

Negligible, and the frames' downstream traversal is an `isinstance` check in
`observability`. Turn-taking also survives: the queue-empty branch still fires
`_bot_stopped_speaking`, so `user_idle_timeout_ms` behaves as it does without a
mixer. **Amended (#265):** true of TTS audio only. A voice that streams its
silence too — gpt-live-1, the video avatars — never empties the queue, and the
transport judges that silence *after* the mix, so the bot never stopped speaking
and the idle guard never armed. `SpeechBeforeAmbience` judges it before the mix.
What is *not* answered by any benchmark is PSTN line echo — ambience
coupling back on the caller's channel and opening a VAD turn. That needs a real
call and is tracked separately (#171; see Consequences for the result).

**2. A wrong sample rate is silent.** `SoundfileMixer` loads a file whose rate
does not match the transport by logging a warning and storing nothing; the mix
then returns its input untouched. The caller hears no ambience, nothing raises,
and no status is wrong. TurnCall runs three output rates — Twilio 8000, WebRTC
16000, WhatsApp 16000 — all named explicitly in the transport factory, never
negotiated. Demonstrated on a real 48 kHz upload:

```
WARNING  Sound file resturant.mp3 has incorrect sample rate 48000 (should be 8000)
48k mp3 on a 8000 Hz transport -> mixed anything: False, peak=0
```

**3. Stereo is silently corrupted.** Two channels at the *right* rate are read
as an interleaved buffer and used as mono: a 3-second stereo file yields 48000
"mono" samples instead of 24000, so it plays at double speed with channels
alternating. Again no error.

**4. The mixer holds the whole file in memory, per call.** Each transport gets
its own loaded copy: 60s @ 16k is 1.92 MB per call (192 MB across 100 concurrent
calls); 300s is 9.6 MB per call and 960 MB across 100.

One more constraint shaped the source of the audio. Shipping a small set of
built-in ambiences in the wheel was the original plan and is not possible on
reasonable terms: Pixabay — the obvious supplier — licenses nothing as CC0 after
2019-01-09, and its Content License forbids distributing content "on a
Standalone basis ... as an ... audio ... or other digital file", where
"resizing" explicitly does not count as the creative effort that would lift the
restriction. Resampling 48k→8k is resizing. Freesound's CC0 filter is a viable
supplier, but the licensing question does not arise at all if TurnCall ships no
audio.

## Decision

**Ambience is one project-owned uploaded sound, normalized on the way in, mixed
into the outbound audio of every voice transport, and off by default.**

**1. No built-in audio. Uploads only.** The licence on the audio is the
uploader's concern, and TurnCall distributes none. This also makes the feature
strictly more useful than a fixed set of rooms would be. Remote URLs are
excluded too: the bytes must be normalized regardless, so a URL is only a
fancier way to start an upload — with an SSRF gate, a fetch on the connect path
where a caller is already listening, and a third party's 404 able to remove
ambience from a working agent.

**2. Renditions are made at upload, never on the way out.** Each sound is
decoded, **forced to mono**, resampled to every rate a voice transport
configures, and stored once per rate. Normalization rather than validation,
because both failure modes above are silent and a validation error a caller
never hears is worse than a conversion they do. The rate set is one constant
that the transport factory and the ingest path share, with a test asserting
every configured transport rate has a rendition — the same silent no-op has now
been reachable twice in this design, and moving the conversion from build time
to upload time does not move the guard with it.

**3. An ambience sound is a project-scoped resource, not an agent's file.** Its
own CRUD, referenced from the agent config by id, refused deletion with 409
while any agent names it. This is the shape knowledge bases and takeaways
already have; agent versioning would otherwise duplicate bytes on every publish,
and "one office recording, twelve agents" is the expected usage. Its read
returns presigned URLs for the renditions, because forced mono at 8 kHz is a
large audible change and otherwise the only way to hear it is to place a call.

**4. The id resolves against a project, and for an inline agent that is the
call's project.** An inline agent config (ADR-0017) arrives from a call-init
webhook with no agent row behind it, so a bare uuid in it is untrusted input. A
stored agent's id is checked at create (422 if it is not in the caller's
project); an inline agent's is resolved against the call's project and, on a
miss, warns and runs without ambience. Playing another project's audio on a call
is a data leak that sounds exactly like the feature working.

**5. Ambience can never end a call.** A missing rendition, a failed fetch, an
unresolvable id: all warn and the call runs silent-roomed. Same rule as one
unusable pronunciation never silencing a reply.

**6. It reaches every voice transport, S2S included.** The mixer lives in the
transport, not the TTS stage, so unlike the video avatar and `tts.pronunciations`
this is not cascade-only. That inversion is recorded here because it will read as
an oversight otherwise.

**7. It is forced off in evals.** An eval isolates an agent's behaviour, and in
audio mode the judge reads a transcription of the agent's output — ambience would
add variance to every audio run for a reason unrelated to the agent. The run
says so in its own `warnings`, because the author reads the run and never the
worker's log.

**8. It is absent from the call recording, and that is kept.** The recorder sits
immediately before the transport in the pipeline and the mixing happens inside
the transport. Moving the recorder to match what the caller heard is an ADR-0005
change for cosmetic gain, and the current arrangement keeps transcripts and
post-call analysis identical to an agent without ambience.

**Amended (#265):** the ambience *is* in the recording, and that is kept. This
decision was stale when written: #178 had moved the recorder *after*
`transport.output()` the same day (ADR-0005), and the frames the transport
passes on are already mixed, so the agent's channel carries the room — measured
on call 55f06d09, a steady −45 dB floor from the first second. Kept rather than
stripped: the recording is now what the caller heard, and recovering the
unmixed audio after the output would cost the real-time pacing #178 moved the
recorder for. Transcripts and post-call analysis read text, so they are still
identical to an agent without ambience.

**9. Renditions are MP3.** Measured on a 30-second mono source: 70 KB at 8k and
127 KB at 16k, against 331 KB / 622 KB as FLAC and 477 KB / 956 KB as WAV, for a
decode of 3.0 ms vs 2.7 ms vs 0.1 ms — paid once per call inside `mixer.start()`,
before the first word. FLAC was the earlier choice on a loop-gap argument that a
discriminating test destroyed: round-tripping a synthetic gapless tone through
libsndfile's MP3 encoder adds 0 samples and 0 ms at either end, so the quiet at
the head of a recording is the recording, not the codec.

**10. Renditions reach the mixer through a process-local cache.** The storage
adapter returns bytes and `SoundfileMixer` wants a path, so something writes a
file. A cache directory keyed by sound and rate, fetch-on-miss, with its own
env var documented as needing a mount — the lesson `EVAL_TTS_CACHE_DIR` already
paid for, where pipecat's default lived under `$HOME` and a container recreate
lost it. No eviction: the cache is bounded by the number of distinct sounds.

**11. Deleting a sound deletes its objects, and so does a project purge.** This
diverges from the existing precedent, which leaks: document deletion and
`hard_delete_project` both remove rows and never touch object storage, so KB
uploads and call recordings are already orphaned forever. The divergence is
deliberate and narrow — an ambience object is referenced by id from a live agent
config, so an orphan here is a thing that looks like it should still work. The
pre-existing leak is filed separately rather than fixed in passing.
*Update 2026-09-30:* that leak is fixed (#168, #183). Document and
knowledge-base deletes now remove their objects too, so this is no longer a
divergence.

**12. Caps: 10 MB upload, 60 s duration.** The size cap and its two-stage check
(`Content-Length` before reading the body, then the actual bytes) match the
knowledge-base endpoint, as do its status codes: 415 for a file type that cannot
be read, 409 for one that is too large. The duration cap is the one with a new
number behind it, and it is the memory table above: 60 s is 1.92 MB per call at
16k, five minutes is 9.6 MB and a capacity incident with no obvious cause.
Ambience loops, so nobody needs more than a minute of it.

**13. No runtime control.** `volume` and `sound` are fixed for the call. pipecat's
`MixerEnableFrame` / `MixerUpdateSettingsFrame` stay available and nothing exposes
them: a built-in tool for this would add an entry to the built-in tool table and
a `call_control` path so that a model can change the room it is in.

## Consequences

- Ambience keeps playing while the voicemail gate holds the agent's speech back,
  so a machine records a few seconds of room tone before the hangup. Accepted:
  the alternative re-introduces the runtime control decision 13 removed, and
  couples two features that currently do not know about each other.
- `volume` defaults to 0.3 with a 0.0–1.0 range. 0.3 rather than pipecat's 0.4
  because an 8 kHz G.711 line carries background energy more intrusively than
  16 kHz WebRTC — reasoning, not a measurement, which is why no ceiling below
  1.0 was invented from it. The real-call echo check is what should move either
  number. *Update 2026-09-30:* the check (#171) found no spurious
  interruptions and no empty turns at 0.3 on a real PSTN call, and a short call
  at 0.75 was clean too, so both numbers stand. The evidence is thin (one
  barge-in call, one short call, one line). Reopen #171 if an agent with
  ambience is ever reported as cutting out.
- `.m4a` is rejected. libsndfile reads 26 formats and AAC is not among them,
  which makes an iPhone voice memo the most likely first upload anyone tries.
  The 415 names both the accepted formats and the fix; an ffmpeg dependency for
  one input format is a system binary in the container, the docs and every
  contributor's setup.
- A content-type allowlist is not sufficient on its own — an `.m4a` sent as
  `audio/mpeg` passes the header check and fails at decode — so the decode
  failure returns the same 415.

## Alternatives considered

- **Built-in ambiences in the wheel.** Blocked on licensing (see Context).
  Freesound CC0 would work; shipping nothing is simpler and strictly less
  legally exposed.
- **Resample on the way out**, one master per sound. Adds conversion to the
  connect path and puts a correctness requirement where a mistake is silent.
- **Subclassing `SoundfileMixer` or `BaseAudioMixer`** to take bytes and skip
  the filesystem. Reimplementing and testing the mix loop to avoid writing a
  250 KB file.
- **Synthesizing ambience** from filtered noise. Zero files and zero licence,
  and it cannot produce convincing murmur — it only covers room tone and line
  hiss, which is not what anyone asked for.
- **Hold audio and one-shot stings.** Deliberately separate concepts. Warm
  transfer does not even pass through this pipeline: ADR-0009 chose `<Number url>`
  whisper precisely to avoid a conference and its hold-music lifecycle, so during
  a briefing the caller is on Twilio and not on us. A sting wants one public 8 kHz
  mulaw URL for `<Play>`, not two mixer-rate renditions, so it is a different
  artifact rather than a mode of this one.
