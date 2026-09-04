# Video Avatar (HeyGen) Example

A cascade voice agent with a live **HeyGen** video avatar, rendered in the
browser over WebRTC. The avatar's lips are driven by the TTS audio.

## What it does

- Cascade pipeline: STT → LLM → TTS → **HeyGen** → browser (audio + video)
- HeyGen runs its own WebRTC to HeyGen's servers and emits avatar video frames
  into the pipeline; TurnCall's SmallWebRTC transport carries them to the browser
- WebRTC + cascade only (no phone, no S2S)

## Prerequisites

1. **Docker**: `make docker-up` (Postgres + Redis)
2. **API keys in `.env`**: `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, and one of
   `HEYGEN_LIVE_AVATAR_API_KEY` (app.liveavatar.com) or `TAVUS_API_KEY` (platform.tavus.io)
3. Server running: `make run`
4. **Tavus only**: `pip install -e .` so the `tavus` extra (`daily-python`) is present

## Run

```bash
# HeyGen (default)
python examples/video-avatar/setup.py --avatar-id <your-liveavatar-id>

# Tavus — higher quality, lower latency
python examples/video-avatar/setup.py --provider tavus --replica-id <your-replica-id>
```

Then open `examples/webrtc-client/index.html`, paste the printed **API key**
and **Agent ID**, and click **Start Call**. The avatar appears below the
buttons once the video track arrives.

## Notes

- No `--avatar-id`? It uses HeyGen's public sandbox avatar.
- Audio-only agents work in the same client — the `<video>` element just stays
  hidden when no video track arrives.
- Want recording or group calls? That needs the Daily transport instead of
  SmallWebRTC — out of scope here (see `adr/0002-heygen-avatar.md`).

## Quick run

```bash
./run.sh
```

All args pass through to `setup.py`.
