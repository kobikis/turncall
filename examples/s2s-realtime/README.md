# Speech-to-Speech (S2S) Example

Ultra-low latency voice agents using native audio-in/audio-out models.
No separate STT or TTS — the model handles everything in a single WebSocket.

## What it does

- Runs a voice agent using **OpenAI Realtime**, **Gemini Live**, or **Grok voice** (`--provider xai`, via an OpenAI-Realtime-compatible gateway) S2S models
- ~300ms time-to-first-byte (vs ~800ms+ for cascade STT->LLM->TTS)
- Natural intonation and emotion in speech output
- Tool/function calling works the same as cascade

## Prerequisites

1. **Docker** (for Postgres + Redis)
2. **One of these API keys:**
   - OpenAI API key (for OpenAI Realtime) — same key as GPT-4o
   - Google API key (for Gemini Live) — get at [aistudio.google.com](https://aistudio.google.com)
   - Vercel AI Gateway key (for Grok voice) — set as `OPENAI_API_KEY` (see [Grok section](#grok-voice-and-other-gateways))
3. **ngrok** (to expose local server to Twilio, if using phone calls)

## Quick Start

### 1. Configure environment

```bash
cp env.example .env
# Edit .env:
#   OPENAI_API_KEY=sk-xxxxxxxx     (for OpenAI Realtime)
#   GOOGLE_API_KEY=AIxxxxxxxx      (for Gemini Live)
#   DEEPGRAM_API_KEY=xxxxxxxx      (still needed for cascade fallback)
```

### 2. Start infrastructure

```bash
make docker-up && make migrate && make run
```

### 3. Run setup script

```bash
# OpenAI Realtime (default)
python examples/s2s-realtime/setup.py --server-url "http://localhost:8090"

# Gemini Live (cheaper, emotion-aware)
python examples/s2s-realtime/setup.py \
  --server-url "http://localhost:8090" \
  --provider google \
  --voice Kore

# Grok voice (presets the Vercel AI Gateway — see the Grok section below)
python examples/s2s-realtime/setup.py \
  --server-url "http://localhost:8090" \
  --provider xai

# With Twilio phone number
python examples/s2s-realtime/setup.py \
  --server-url "https://xxxx.ngrok.io" \
  --twilio-number "+15559876543" \
  --twilio-number-sid "PNxxxxxxxx"
```

### 4. Talk to it!

- **Browser**: Open `examples/webrtc-client/index.html`
- **Phone**: Call your Twilio number

## Provider Comparison

| | OpenAI Realtime | Gemini Live |
|---|---|---|
| **Latency** | ~300ms TTFB | ~300ms TTFB |
| **Cost** | ~$0.06/min in, $0.24/min out | ~$0.04/min |
| **Voices** | alloy, ash, ballad, coral, echo, sage, shimmer, verse | Aoede, Charon, Fenrir, Kore, Leda, Orus, Puck, Zephyr (+ more — Gemini validates on connect) |
| **Emotion** | Natural intonation | Affective dialog support |
| **Thinking** | No | Yes (`<|think|>` tokens) |
| **Tool calling** | Full support | Full support |
| **Model** | `gpt-realtime-2.1` | `models/gemini-3.1-flash-live-preview` |

## Grok voice and other gateways

`--provider xai` is a shorthand: the backend has no xai S2S provider — Grok
speaks the **OpenAI-Realtime WebSocket protocol**, so the agent is created with
`s2s.provider: "openai"` plus these presets (each overridable by flag):

| Preset | Value | Override |
|---|---|---|
| `base_url` | `wss://ai-gateway.vercel.sh/v1/realtime` | `--base-url` |
| `model` | `xai/grok-voice-think-fast-1.0` | `--model` |
| `voice` | `cosmo` | `--voice` |

**Two env prerequisites** (setup.py checks your `.env` and warns, but the
server enforces them — the allowlist at *call start*, not agent creation):

1. **Allowlist the gateway URL.** `base_url` is an outbound target, so it's
   gated by the same SSRF guard as custom LLMs — add its `wss://` to
   `BYOM_ALLOWED_URL_PATTERNS`, e.g. `["wss://ai-gateway.vercel.sh/*"]`.
2. **Use the gateway key.** Set `OPENAI_API_KEY` to the gateway's key (sent as
   `Authorization: Bearer …` on the WebSocket). While it holds the gateway key,
   plain-openai S2S agents can't run — the backend has one `OPENAI_API_KEY`.

Any other OpenAI-Realtime-compatible endpoint (LiteLLM, xAI direct) works the
same way — pass `--base-url` explicitly (with `--model`, since the gateway
routes to models we can't guess):

```jsonc
"s2s": {
  "provider": "openai",
  "base_url": "wss://ai-gateway.vercel.sh/v1/realtime",
  "model": "xai/grok-voice-think-fast-1.0",   // openai/gpt-realtime-2 also works
  "voice": "cosmo"                             // the gateway validates its own voices
}
```

When `base_url` is set the OpenAI voice allowlist is bypassed (the gateway
routes to models with their own voice sets), so pass the model's own voice name.

## How it differs from cascade

```
Cascade:  Audio → STT (Deepgram) → LLM (OpenAI) → TTS (ElevenLabs) → Audio
          3 network hops, ~800-1200ms

S2S:      Audio → [Single Model WebSocket] → Audio
          1 persistent connection, ~300-500ms
```

The S2S model receives raw audio, understands speech, reasons, and generates
speech — all internally. No intermediate text representation.

## Architecture

```
Phone/Browser → Transport → Audio frames
  → OpenAI Realtime WebSocket (or Gemini Live)
  → Audio frames → Transport → Phone/Browser

  Side channels:
  → TranscriptionFrame → Observability (DB logging)
  → FunctionCall → Tool Bridge → Webhook/Built-in
```

## Quick run

```bash
./run.sh
```

Reads `PUBLIC_BASE_URL` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
