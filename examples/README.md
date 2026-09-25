# Examples

Seventeen runnable examples. Each is a directory with a `README.md`, a `setup.py`
that creates the project, agent and bindings through the API, and a `run.sh`
that reads what it needs from the repo-root `.env`.

Start the stack first — see the [Quickstart](../QUICKSTART.md):

```bash
make docker-up-local && make migrate-local
```

## Start here

These need **no phone number and no tunnel**, so they cost nothing and work in
about a minute.

| Example | What it shows |
|---|---|
| [`webrtc-client`](webrtc-client/) | Talk to an agent in your browser — the fastest way to hear one |
| [`receptionist`](receptionist/) | A dental clinic receptionist: intent, booking, transfer |
| [`knowledge-base`](knowledge-base/) | All three RAG retrieval modes on one agent |
| [`video-avatar`](video-avatar/) | A lip-synced video avatar on a WebRTC call |

## Providers and pipelines

| Example | What it shows |
|---|---|
| [`s2s-realtime`](s2s-realtime/) | Speech-to-speech: OpenAI Realtime, Gemini Live, Grok via a gateway |
| [`bedrock`](bedrock/) | AWS Bedrock as the LLM, or Amazon Nova Sonic 2 as S2S |
| [`ollama-local`](ollama-local/) | A local LLM — nothing but the telco leaves your machine |
| [`openrouter-fallback`](openrouter-fallback/) | Model fallback routing when the primary rate-limits |

## Tools and integrations

| Example | What it shows |
|---|---|
| [`tools-showcase`](tools-showcase/) | Every tool type: built-in, custom webhook, and their invocation records |
| [`mcp-tools`](mcp-tools/) | MCP servers, with tools discovered at call start |
| [`call-transfer`](call-transfer/) | Cold and warm transfer, including the operator briefing |
| [`events-webhook`](events-webhook/) | Receive and verify every event TurnCall emits |
| [`eval-tool-mock`](eval-tool-mock/) | An eval that books nothing: tool mocks, and why they fail closed |

## Channels

| Example | What it shows |
|---|---|
| [`sms-chat`](sms-chat/) | One agent answering both calls and SMS |
| [`whatsapp`](whatsapp/) | WhatsApp voice and text on one number |

## Operating a fleet

| Example | What it shows |
|---|---|
| [`ab-testing`](ab-testing/) | Two agent versions on one number, split deterministically by caller |
| [`post-call-analysis`](post-call-analysis/) | Summary, sentiment, scoring and structured extraction after a call |

## What each one needs

Everything needs `OPENAI_API_KEY` and `DEEPGRAM_API_KEY` in the repo-root
`.env`. Beyond that:

| Also needs | Examples |
|---|---|
| A Twilio number + `ngrok` | `ab-testing`, `call-transfer`, `events-webhook`, `mcp-tools`, `ollama-local`, `openrouter-fallback`, `post-call-analysis`, `receptionist`, `s2s-realtime`, `sms-chat`, `tools-showcase` |
| `ngrok` only | `whatsapp` |
| Nothing else | `webrtc-client`, `knowledge-base`, `video-avatar`, `eval-tool-mock` |
| A provider key of its own | `bedrock` (AWS), `video-avatar` (HeyGen or Tavus), `openrouter-fallback` (OpenRouter), `ollama-local` (Ollama running locally) |

Most phone examples also accept `--twilio-number` and `--twilio-number-sid` as
optional flags — omit them and you get a browser-callable agent instead, which
is usually enough to see the feature work.

## Running one

```bash
./examples/<name>/run.sh              # reads PUBLIC_BASE_URL etc. from ../.env
python3 examples/<name>/setup.py --help   # or drive setup.py directly
```

`run.sh` tells you which variables are missing rather than failing part-way
through, so a missing `TWILIO_PN_SID` stops before anything is created.
