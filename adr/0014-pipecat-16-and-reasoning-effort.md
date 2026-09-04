# Pipecat 1.5 → 1.6 + `reasoning_effort` LLM config

We bumped the `pipecat-ai` pin from `>=1.5.0` to `>=1.6.0` (same major, mostly
additive) and added an OpenAI `reasoning_effort` knob to the agent LLM config.
This records the scope and the paths deliberately left out.

## Pipecat 1.6 bump

- **Pin** to `>=1.6.0,<2.0.0` (same extras). The old range already permitted 1.6;
  the image was just resolved to 1.5.0.
- **No code changes for breaking changes.** 1.6's three breaking changes don't
  touch TurnCall: RTVI DTMF `button`→`buttons` (we don't use RTVI DTMF —
  `send_dtmf` is server-side TwiML), the dropped `pyyaml-include` dep (no Pipecat
  Flows / YAML `!include`), and OTel span-attribute renames (no span-name
  assertions in code or tests).
- **Not adopted:** `OpenAIResponsesLLMService` (1.6's native reasoning path). It
  is OpenAI-only (would break the openrouter/custom_openai branches) and swapping
  the core LLM service means re-validating streaming, tools, and interruptions.
  See the reasoning decision below.

## `reasoning_effort`

- New optional field `llm.reasoning_effort` ∈ `{minimal, low, medium, high}`,
  default unset. OpenAI-family only (`openai`, `openrouter`, `custom_openai`);
  applies on the **voice cascade** and **chat/SMS** paths.
- **Delivered via `extra_body`** on the existing chat-completions
  `OpenAILLMService` — the same mechanism OpenRouter fallback `models` already
  use. Works on 1.5 and 1.6, no service swap, covers all OpenAI-compatible
  providers.
- **Only sent when set.** No model allow-list gating: OpenAI's reasoning-model
  list drifts, so an allow-list would rot and reject valid new models. Setting it
  on a non-reasoning model surfaces the provider's own 400.

## Deliberately out of scope

- **Anthropic / Gemini / Ollama.** `reasoning_effort` is an OpenAI parameter;
  Anthropic/Gemini use a different "thinking budget" shape. Not mapped.
- **S2S** (OpenAI Realtime / Gemini Live) — neither accepts `reasoning_effort`.
- **Post-call analysis + voicemail classifier.** Deterministic background jobs
  pinned to fixed sampling; reasoning would add latency/cost for no benefit. The
  voicemail classifier reuses `_create_llm_service`, so it explicitly suppresses
  the field.
