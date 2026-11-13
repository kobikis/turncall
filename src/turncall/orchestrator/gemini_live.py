"""Gemini Live's pipecat service as TurnCall builds it: a handoff reopens it.

pipecat 1.16 applies neither a new system instruction nor an
`LLMSetToolsFrame` to an open Gemini Live session (both TODOs there), so after
a handoff the model went on as the previous agent, calling tools whose
handlers were gone, until the session next reopened (#464). A handoff sends
the target's prompt and then its tools (`tool_bridge.build_switch_frames`),
so the tools frame is where the session is reopened, with both.

Imports pipecat at module level, so `s2s_config` imports it only when it
builds a `google` agent's service.
"""

from typing import Any

from pipecat.frames.frames import Frame, LLMSetToolsFrame
from pipecat.processors.aggregators.llm_context import LLMContext, is_given
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService


class HandoffGeminiLive(GeminiLiveLLMService):
    """Gemini Live, reopened on the target agent's prompt and tools."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMSetToolsFrame):
            await self._reopen_with(frame.tools)

    async def _reopen_with(self, tools: Any) -> None:
        # The session's tools are the context's when it has some, else the
        # ones it was built with (#459): a target with none would get the
        # first agent's back, so its own replace those too.
        tools = LLMContext._normalize_and_validate_tools(
            tools, allow_provider_tools=True
        )
        self._tools_from_init = tools if is_given(tools) else None
        # A resumed session keeps the instruction and tools it was opened
        # with, whatever the new one asks for (measured): so a new session,
        # seeded from the context — which the handoff cleared, as on cascade.
        self._session_resumption_handle = None
        await self._reconnect()
