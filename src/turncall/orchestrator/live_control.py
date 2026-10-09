"""What a live call does with the API's control requests (#215).

The channel itself — subscribe, answer, acknowledge — is
`services/live_call_channel`. This is the call's half: which request kinds it
answers and what each does to its pipeline. `CallSession.start()` holds the
channel open for exactly as long as it runs the call, which is what makes it
every voice transport's: Twilio, WebRTC and WhatsApp voice all end in that one
method (`tests/unit/test_live_control_wiring.py` fails on one that does not).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame, LLMMessagesAppendFrame

from turncall.orchestrator.pipeline_factory import CallContext
from turncall.services import live_call_channel
from turncall.services.live_call_channel import MESSAGE, ControlRefused, Handler

QueueFrame = Callable[[Frame], Awaitable[None]]


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


def call_handlers(queue_frame: QueueFrame, *, pipeline_mode: str) -> dict[str, Handler]:
    """Every request kind a call answers. #216 adds the handoff here."""
    return {MESSAGE: message_handler(queue_frame, pipeline_mode=pipeline_mode)}


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
