# Pipecat 1.0 → 1.4: safe bump, scoped feature adoption

We upgraded the `pipecat-ai` pin from `>=1.0.0` to `>=1.4.0` (same major, mostly additive). We deliberately adopted only one new feature and deliberately *declined* several others, so this records the choices to stop them being relitigated.

## What we adopted

- **Pin bump** to `>=1.4.0,<2.0.0`. Gets Smart Turn v3's memory/cold-start win (~566MB→60MB, ~5s→0.3s) for free — bit-identical output, no code change.
- **`realtime_service_mode=True`** on the S2S `LLMContextAggregatorPair` (`orchestrator/pipeline_factory.py`). Pipecat's current recommended wiring for realtime (OpenAI Realtime / Gemini Live) services: context writes become trailing and turn strategies auto-swap based on whether the service emits its own turn frames. Orthogonal to `vad_analyzer`, so `pipecat_vad` mode keeps its `SileroVADAnalyzer`.
- **`TTSSpeakFrame(append_to_context=False)`** on the voicemail message push. Forced, not optional: 1.4 flipped the default to `True`, which would otherwise leak the voicemail prompt into LLM context.

## What we deliberately did NOT adopt

- **1.4 direct-function tool auto-registration** (`LLMContext(tools=[direct functions])` collapsing `_build_tools_schema` + `register_tools`). Pure refactor, no new behavior, and it touches the tool path + MCP discovery timing. Not worth the surface area. The two-phase split (schema built in `pipeline_factory`, handlers registered in `tool_bridge` after MCP discovery) stays on purpose.
- **`add_tool_change_messages`** on the LLM aggregator. It only fires on `LLMSetToolsFrame`, which is emitted nowhere — `handoff_to_agent` swaps the system prompt (`context.set_messages`) but not tools. The flag would be a no-op.
- **WebSocket HMAC token auth / origin restriction** (1.4). The media-stream WS sits behind Twilio's own auth; not needed.

## Known gap (separate from this upgrade)

`handoff_to_agent` (`orchestrator/tool_bridge.py`) applies the target agent's prompt but not its tool set. Pre-existing; out of scope here. If/when that's fixed to swap tools mid-call, `add_tool_change_messages` becomes worth revisiting.