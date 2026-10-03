"""One goodbye when gpt-live-1's client delegate ends the call (#262).

gpt-live-1 is told to leave the goodbye to the delegation (`s2s_config`), and
may still fill the wait ("One moment"). So the delegate's tool only marks the
call as ending, and the live service arms the gate as it relays the
delegate's answer — once gpt-live-1 is quiet, so the goodbye is a reply of its
own and the one the call ends after. Driven at the built live service, as
#247's tests are.
"""

from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from pipecat.services.openai.live import events
from pipecat.workers.llm.backend_llm_worker import BackendOutput

from tests.unit.test_delegation_completed import (
    _BOOK,
    _LLM_MODULE,
    _answers,
    _call_context,
    _delegate,
    _live,
    _payloads,
    _raises,
    _silent,
)
from turncall.orchestrator import delegation_events
from turncall.orchestrator.hangup import HangupAfterReply

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def stored() -> Any:
    with patch.object(delegation_events, "store_call_event", new=AsyncMock()) as store:
        yield store


async def _thinks_only(*_: Any, on_update: Any, **__: Any) -> str:
    """Said goodbye beside the tool call, then nothing: progress, unspoken."""
    await on_update(BackendOutput(text="Goodbye!", prefers_spoken=False))
    return ""


async def _run(backend: Any, *, ending: bool = True) -> list[Any]:
    """What reached the gate and gpt-live-1, in order."""
    seen: list[Any] = []

    async def _arm_when_quiet(*_: Any) -> None:
        seen.append("arm when quiet")

    async def _queue(_gate: Any, frame: Any, *_: Any) -> None:
        seen.append(("arm", frame.current_reply))

    live = _live({}, _call_context(), tools=[_BOOK])
    live.send_client_event.side_effect = lambda event: seen.append(type(event))
    with (
        patch.object(HangupAfterReply, "ending", new=PropertyMock(return_value=ending)),
        patch.object(HangupAfterReply, "arm_when_quiet", new=_arm_when_quiet),
        patch.object(HangupAfterReply, "queue_frame", new=_queue),
        patch(f"{_LLM_MODULE}._delegate_to_backend", new=backend),
    ):
        await _delegate(live)
    return seen


async def test_the_delegates_goodbye_is_the_reply_the_call_ends_after(
    stored: AsyncMock,
) -> None:
    seen = await _run(_answers("Goodbye! Have a great day!"))

    # Armed for the reply after a filler gpt-live-1 may still be saying.
    assert seen == ["arm when quiet", events.SessionCommentaryAppendEvent]
    assert [p["outcome"] for p in _payloads(stored)] == ["answered"]


@pytest.mark.parametrize(
    ("backend", "outcome"),
    [
        # It ended the call, which is what it was asked: #247's `failed`
        # reads an empty answer as a delegation that did nothing.
        (_silent, "answered"),
        (_thinks_only, "answered"),
        (_raises(RuntimeError("boom")), "failed"),
    ],
)
async def test_with_nothing_to_voice_the_call_ends_after_the_reply_playing(
    stored: AsyncMock, backend: Any, outcome: str
) -> None:
    """pipecat's stand-in sentence is not the caller's last words."""
    seen = await _run(backend)

    assert seen == [events.SessionThinkingAppendEvent, ("arm", True)]
    assert [p["outcome"] for p in _payloads(stored)] == [outcome]


async def test_a_call_not_ending_is_relayed_as_ever(stored: AsyncMock) -> None:
    seen = await _run(_silent, ending=False)

    assert seen == [events.SessionCommentaryAppendEvent]
    assert [p["outcome"] for p in _payloads(stored)] == ["failed"]
