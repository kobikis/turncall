# 0003 — Dedicated OpenRouter LLM provider

OpenRouter is an OpenAI-compatible endpoint, so it already works today through the
generic `custom_openai` provider (`base_url: "https://openrouter.ai/api/v1"` + a
per-agent key). We nonetheless added a **first-class `openrouter` provider** because
the driver is **model fallback routing** — OpenRouter's request-body `models` array,
which tries an ordered list of models and fails over when the primary rate-limits or
errors mid-call. We chose to expose that as a validated, first-class `fallback_models`
config field rather than a raw `extra`/`extra_body` passthrough, and a feature that
prominent shouldn't hang off the raw BYOM escape hatch — so it gets its own provider
that bakes in the base URL and reads a platform-level `OPENROUTER_API_KEY`.

## Considered options

- **`custom_openai` + `extra` passthrough** (lazy): one-line factory change to forward
  `extra` into `OpenAILLMService` settings; users hand-write `extra_body: {models: [...]}`
  and the magic base URL. Rejected — fallback is a headline feature, not a power-user knob.
- **Dedicated `openrouter` provider + structured `fallback_models`** (chosen): ergonomic,
  validated, fails fast on misconfiguration.

## Consequences

- Fallback routing is built on Pipecat's `OpenAILLMService` (`extra` → merged into
  `chat.completions.create`, sent as `extra_body`). No new Pipecat dependency.
- `fallback_models` is rejected at API ingest on any provider other than `openrouter`.
- **No customer text conversations for v1.** Customer SMS/Chat/WhatsApp-text
  conversations reject `provider: "openrouter"` — the block lives at the chat boundary
  (`services/sms_chat.py:_process_chat_message`), not in `complete_text`. Fallback is a
  real-time reliability story, and the per-turn model-attribution tap relies on the frame
  pipeline, which the text conversation path doesn't have. Extend to text conversations
  only when a text agent needs it.
- **Internal completions are allowed.** `complete_text` (`services/llm_text.py`) fully
  supports `openrouter` — resolving `https://openrouter.ai/api/v1` + `OPENROUTER_API_KEY`
  and sending the `models` fallback array — so internal callers like post-call analysis
  work for openrouter voice agents. (Guarding `complete_text` itself originally broke
  analysis; the scope is per-use-case, not per-function.)
- The actually-used model is captured per turn via `OpenAILLMService.get_full_model_name()`
  (Pipecat already reads it off the response) and recorded on the assistant transcript event.
