# Local Ollama Example

A voice agent powered by a local LLM running on your machine via Ollama.
No OpenAI API key needed for the LLM — just Ollama with any model you want.

## What it does

- Runs a voice agent using a **local LLM** (Gemma 4, Llama 3, Phi-3, etc.)
- STT and TTS still use cloud providers (Deepgram by default)
- Demonstrates the BYOM (Bring Your Own Model) feature

## Prerequisites

1. **Ollama** installed and running: https://ollama.com
2. A model pulled: `ollama pull gemma3:12b` (or any model you prefer)
3. **Deepgram API key** (for STT/TTS — free tier available)
4. **Docker** (for Postgres + Redis)
5. **ngrok** (to expose local server to Twilio, if using phone calls)

## Quick Start

### 1. Pull a model

```bash
ollama pull gemma3:12b
# Verify it's running:
curl http://localhost:11434/v1/models
```

### 2. Configure environment

```bash
cp env.example .env
# Edit .env:
#   DEEPGRAM_API_KEY=your-deepgram-key
#   (OPENAI_API_KEY is NOT required for the LLM)
```

### 3. Start infrastructure

```bash
make docker-up    # postgres + redis
make migrate      # create database tables
make run          # start server
```

### 4. Run setup script

```bash
# WebRTC only (browser calls, no Twilio needed):
python examples/ollama-local/setup.py --server-url "http://localhost:8090"

# With Twilio phone number:
python examples/ollama-local/setup.py \
  --server-url "https://xxxx.ngrok.io" \
  --twilio-number "+15559876543" \
  --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### 5. Talk to it!

- **Browser**: Open the WebRTC client at `examples/webrtc-client/index.html`
- **Phone**: Call your Twilio number

## Supported Models

Any model Ollama can serve works. Recommended for voice (need fast responses):

| Model | Size | VRAM | Notes |
|-------|------|------|-------|
| `gemma3:4b` | 4B | ~3GB | Fast, good for simple tasks |
| `gemma3:12b` | 12B | ~8GB | Best balance of speed and quality |
| `llama3.1:8b` | 8B | ~5GB | Good general purpose |
| `phi3:mini` | 3.8B | ~3GB | Very fast, decent quality |
| `mistral:7b` | 7B | ~5GB | Strong reasoning |

## Using a Remote OpenAI-Compatible Endpoint

You can also use any OpenAI-compatible API (Together AI, Groq, Fireworks, vLLM, etc.):

```bash
python examples/ollama-local/setup.py \
  --server-url "http://localhost:8090" \
  --llm-provider custom_openai \
  --llm-model "meta-llama/Llama-3-70b-chat-hf" \
  --llm-base-url "https://api.together.xyz/v1" \
  --llm-api-key "your-together-key"
```

## Architecture

```
Phone/Browser → Transport → Audio → STT (Deepgram cloud)
  → LLM (local Ollama @ localhost:11434) → TTS (Deepgram cloud) → Audio
```

The LLM runs locally — only STT and TTS hit the cloud.

## Quick run

```bash
./run.sh
```

Reads `PUBLIC_BASE_URL` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
