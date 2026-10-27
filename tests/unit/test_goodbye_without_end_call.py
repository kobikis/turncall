"""A goodbye gpt-live-1 never ends the call on (#413).

Call 64c71433: the caller said bye while a booking was being delegated,
gpt-live-1 answered "Sure, goodbye." itself, and no `end_call` followed — the
line stayed open for 35 s and four more byes. Once the caller and gpt-live-1
have both said goodbye and nothing is in flight, the call waits for an ending
tool and then ends itself, recorded as the agent's end. Driven at the built
live service, as #247's and #262's tests are.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from pipecat.services.openai.live import events
from pipecat.workers.llm.backend_llm_worker import BackendIdle, BackendOutput

from tests.unit.test_delegation_completed import _BOOK, _created, _live
from turncall.orchestrator import delegation_events
from turncall.orchestrator.delegation_events import GOODBYE_WITHOUT_END_CALL
from turncall.orchestrator.hangup import HangupAfterReply
from turncall.orchestrator.pipeline_factory import CallContext

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_END_CALL = {
    "name": "end_call",
    "description": "End the call.",
    "parameters_schema": {"type": "object", "properties": {}},
}
_BYE = ("caller", "Okay, thank you. I need to go. Bye bye.")
_ITS_BYE = ("gpt-live-1", "Sure, goodbye.")


def _said(who: str, text: str) -> events.TranscriptDeltaEvent:
    kind = "input" if who == "caller" else "output"
    return events.TranscriptDeltaEvent(
        type=f"session.{kind}_transcript.delta", delta=text, start_ms=0, end_ms=0
    )


_WAIT = 0.2


async def _drain(live: Any) -> None:
    """Turn gaps, waits and delegations, until nothing is left running."""
    for _ in range(20):
        await asyncio.gather(*live.tasks, return_exceptions=True)
        await asyncio.sleep(0)


async def _run(
    *script: Any,
    tools: list[dict[str, Any]] | None = None,
    ending: bool = False,
    is_eval: bool = False,
) -> tuple[list[bool], AsyncMock]:
    """What reached the gate (`current_reply` per arm) and what was recorded."""
    armed: list[bool] = []

    @asynccontextmanager
    async def _session() -> Any:
        yield AsyncMock()

    async def _queue(_gate: Any, frame: Any, *_: Any) -> None:
        armed.append(frame.current_reply)

    context = CallContext(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="CA1",
        stream_sid="MZ1",
        session_factory=_session,
        eval_run_id=uuid.uuid4() if is_eval else None,
    )
    live = _live({}, context, tools=[_BOOK, _END_CALL] if tools is None else tools)
    live._user_turn.gap_secs = live._assistant_turn.gap_secs = 0
    with (
        patch.object(delegation_events, "_UNENDED_GOODBYE_SECS", _WAIT),
        patch.object(delegation_events, "store_call_event", new=AsyncMock()),
        patch(
            "turncall.services.call_control.begin_assistant_end", new=AsyncMock()
        ) as recorded,
        patch.object(HangupAfterReply, "ending", new=PropertyMock(return_value=ending)),
        patch.object(HangupAfterReply, "queue_frame", new=_queue),
    ):
        for step in script:
            if step == "delegate":
                await _created(live)
            elif step == "wait out":
                await asyncio.sleep(_WAIT * 1.5)
            elif isinstance(step, tuple):
                await live._handle_evt_transcript_delta(_said(*step))
                await asyncio.sleep(
                    0.02
                )  # the turn closes (gap 0), well within the wait
            else:
                await live._on_backend_event(step)
        await _drain(live)
    return armed, recorded


async def test_both_goodbyes_and_no_end_call_end_the_call() -> None:
    armed, recorded = await _run(_BYE, _ITS_BYE)

    # After the reply playing, recorded first as the agent's end.
    assert armed == [True]
    assert recorded.await_args.kwargs["reason"] == GOODBYE_WITHOUT_END_CALL


async def test_a_goodbye_during_a_delegation_ends_the_call_after_its_relay() -> None:
    """The 64c71433 shape: the wait does not end the call on the work in flight,
    whose answer the caller still hears; its end starts the wait again."""
    armed, recorded = await _run(
        "delegate",
        _BYE,
        _ITS_BYE,
        "wait out",  # with the booking still in flight
        BackendOutput(text="Your table for six is booked."),
        BackendIdle(),
    )

    assert armed == [True]
    assert recorded.await_count == 1


@pytest.mark.parametrize(
    "said",
    [
        # gpt-live-1 fills the wait while it hands end_call over: no goodbye.
        [_BYE, ("gpt-live-1", "One moment.")],
        # Its goodbye with nothing from the caller.
        [("caller", "That's all."), ("gpt-live-1", "Have a lovely evening!")],
        # The caller went on after both goodbyes.
        [_BYE, _ITS_BYE, ("caller", "Oh wait, one more thing.")],
    ],
)
async def test_without_both_goodbyes_the_call_goes_on(
    said: list[tuple[str, str]],
) -> None:
    armed, recorded = await _run(*said)

    assert armed == []
    recorded.assert_not_awaited()


async def test_an_end_call_already_running_ends_the_call_its_own_way() -> None:
    armed, recorded = await _run(_BYE, _ITS_BYE, ending=True)

    assert armed == []
    recorded.assert_not_awaited()


@pytest.mark.parametrize(
    ("tools", "is_eval"),
    [
        # An agent that cannot end a call is not made to.
        ([_BOOK], False),
        # An eval has no hangup gate: the harness owns its length.
        (None, True),
    ],
)
async def test_off_where_nothing_ends_the_call(
    tools: list[dict[str, Any]] | None, is_eval: bool
) -> None:
    armed, recorded = await _run(_BYE, _ITS_BYE, tools=tools, is_eval=is_eval)

    assert armed == []
    recorded.assert_not_awaited()
