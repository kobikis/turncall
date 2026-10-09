"""What a live call does with the API's control requests (#215, #216).

The channel itself — subscribe, answer, acknowledge — is
`services/live_call_channel`. This is the call's half: which request kinds it
answers and what each does to its pipeline. `CallSession.start()` holds the
channel open for exactly as long as it runs the call, which is what makes it
every voice transport's: Twilio, WebRTC and WhatsApp voice all end in that one
method (`tests/unit/test_live_control_wiring.py` fails on one that does not).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, NamedTuple

from loguru import logger
from pipecat.frames.frames import Frame, LLMMessagesAppendFrame

from turncall.orchestrator.pipeline_factory import CallContext
from turncall.orchestrator.tool_bridge import advertised_tool_names, run_handoff
from turncall.services import live_call_channel
from turncall.services.live_call_channel import (
    HANDOFF,
    MESSAGE,
    ControlRefused,
    Handler,
)

QueueFrame = Callable[[Frame], Awaitable[None]]

# The schema refuses `handoff_to_agent` on these for the same reason.
OPENAI_LIVE_HANDOFF_REFUSAL = (
    "A handoff is not supported on openai_live: it would swap the delegate's "
    "tools but not gpt-live-1's session, leaving the previous agent's voice and "
    "prompt in place"
)


def message_handler(queue_frame: QueueFrame, *, pipeline_mode: str) -> Handler:
    """Add an API message to the model's context, for its next turn.

    Cascade only. A realtime model is configured from the context it starts
    with and then reads only function results from it — pipecat's OpenAI
    Realtime service logs "NEED TO IMPLEMENT MESSAGES APPEND", and gpt-live-1
    reads no context after its first — so on S2S the message would land in a
    context nothing reads. Refused there, so the API says so instead of 200.
    """

    async def _append(body: dict[str, Any]) -> dict[str, Any]:
        if pipeline_mode == "s2s":
            raise ControlRefused(
                "A speech-to-speech call does not read messages added after it "
                "starts; the message was not applied"
            )
        role, content = body["role"], body["message"]
        await queue_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": role, "content": content}], run_llm=False
            )
        )
        return {"role": role}

    return _append


class CallStages(NamedTuple):
    """What a handoff switches — the three things a tool call's
    `FunctionCallParams` carries, found in the running pipeline instead."""

    llm: Any
    context: Any
    pipeline_worker: Any


def _all(processor: object) -> Iterator[object]:
    for child in getattr(processor, "processors", []):
        yield child
        yield from _all(child)


def stages_of(pipeline: object, worker: object) -> CallStages | None:
    """The call's LLM (a failover member resolves to its group when the
    handoff registers tools), its context, and the worker running it."""
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
    )
    from pipecat.services.llm_service import LLMService

    processors = list(_all(pipeline))
    llm = next((p for p in processors if isinstance(p, LLMService)), None)
    user = next((p for p in processors if isinstance(p, LLMUserAggregator)), None)
    if worker is None or llm is None or user is None:
        return None
    return CallStages(llm=llm, context=user.context, pipeline_worker=worker)


def handoff_handler(
    call_context: CallContext,
    stages: Callable[[], CallStages | None],
    *,
    pipeline_mode: str,
    s2s_provider: str | None,
) -> Handler:
    """Hand the call off to another agent, through the same prepare / commit /
    apply path as the model's `handoff_to_agent` tool (#144), so the record and
    the pipeline agree. Refused wherever that tool is refused."""

    async def _handoff(body: dict[str, Any]) -> dict[str, Any]:
        if pipeline_mode == "s2s" and s2s_provider == "openai_live":
            raise ControlRefused(OPENAI_LIVE_HANDOFF_REFUSAL)
        found = stages()
        if found is None:
            raise ControlRefused("The call is not running")
        outcome = await run_handoff(body, call_context, found)
        result = json.loads(outcome.result)
        if outcome.applied is None:
            raise ControlRefused(result["message"])
        return {
            **(result.get("details") or {}),
            "tools": advertised_tool_names(outcome.applied),
        }

    return _handoff


def call_handlers(
    queue_frame: QueueFrame,
    call_context: CallContext,
    stages: Callable[[], CallStages | None],
    *,
    pipeline_mode: str,
    s2s_provider: str | None,
) -> dict[str, Handler]:
    """Every request kind a call answers."""
    return {
        MESSAGE: message_handler(queue_frame, pipeline_mode=pipeline_mode),
        HANDOFF: handoff_handler(
            call_context,
            stages,
            pipeline_mode=pipeline_mode,
            s2s_provider=s2s_provider,
        ),
    }


@asynccontextmanager
async def control_channel(
    call_context: CallContext, handlers: dict[str, Handler]
) -> AsyncIterator[None]:
    """Hold the call's control channel open for the block.

    None in an eval, which is no call: there is nothing for the API to reach.
    Without Redis (not initialized, or unreachable) the call runs without a
    channel and every request to it is a 409 — a call is never lost to it.
    """
    if call_context.is_eval:
        yield
        return
    from turncall.storage.redis import dedicated_client, get_redis

    try:
        redis, subscriber = get_redis(), dedicated_client()
    except RuntimeError:
        logger.warning("control_channel_no_redis", call_id=str(call_context.call_id))
        yield
        return
    async with live_call_channel.serve(
        redis, subscriber, call_context.call_id, handlers
    ):
        yield
