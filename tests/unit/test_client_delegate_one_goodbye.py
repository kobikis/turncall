"""One goodbye when gpt-live-1's client delegate ends the call (#262).

gpt-live-1 is told to leave the goodbye to the delegation (`s2s_config`), and
may still fill the wait ("One moment"). So the delegate's tool only marks the
call as ending; what the delegate says for the caller after that is held until
the delegation finishes — its output is a stream, and a second message must not
be cut off by a hangup armed on the first — then relayed as one, with the gate
armed once gpt-live-1 is quiet, so the goodbye is a reply of its own and the
one the call ends after. Driven at the built live service, as #247's tests are.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from pipecat.services.openai.live import events
from pipecat.workers.llm.backend_llm_worker import (
    BackendError,
    BackendIdle,
    BackendOutput,
)

from tests.unit.test_delegation_completed import (
    _BOOK,
    _call_context,
    _created,
    _live,
    _payloads,
    _settle,
)
from turncall.orchestrator import delegation_events
from turncall.orchestrator.delegation_events import NO_OUTPUT, NOT_COMPLETED
from turncall.orchestrator.hangup import HangupAfterReply

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_COMMENTARY = events.SessionCommentaryAppendEvent
_THINKING = events.SessionThinkingAppendEvent


@pytest.fixture
def stored() -> Any:
    with patch.object(delegation_events, "store_call_event", new=AsyncMock()) as store:
        yield store


async def _run(
    *reported: Any, ending: bool = True, ended_before: bool = False
) -> list[Any]:
    """What reached the gate and gpt-live-1, in order. The delegate's tool
    marks the call as ending once the delegation is under way (`ending`), or
    the call was ending before it started (`ended_before`)."""
    seen: list[Any] = []
    state = {"ending": ended_before}

    async def _arm_when_quiet(*_: Any) -> None:
        seen.append("arm when quiet")

    async def _queue(_gate: Any, frame: Any, *_: Any) -> None:
        seen.append(("arm", frame.current_reply))

    live = _live({}, _call_context(), tools=[_BOOK])
    live.send_client_event.side_effect = lambda event: seen.append(
        (type(event), event.content)
    )
    with (
        patch.object(
            HangupAfterReply,
            "ending",
            new=PropertyMock(side_effect=lambda: state["ending"]),
        ),
        patch.object(HangupAfterReply, "arm_when_quiet", new=_arm_when_quiet),
        patch.object(HangupAfterReply, "queue_frame", new=_queue),
    ):
        await _created(live)
        state["ending"] = state["ending"] or ending
        for event in reported:
            if event == "wait":
                for _ in range(5):
                    await asyncio.sleep(0)
            else:
                await live._on_backend_event(event)
        await _settle(live)
    return seen


async def test_the_delegates_goodbye_is_the_reply_the_call_ends_after(
    stored: AsyncMock,
) -> None:
    seen = await _run(BackendOutput(text="Goodbye! Have a great day!"), BackendIdle())

    # Armed for the reply after a filler gpt-live-1 may still be saying.
    assert seen == ["arm when quiet", (_COMMENTARY, "Goodbye! Have a great day!")]
    assert [p["outcome"] for p in _payloads(stored)] == ["answered"]


async def test_everything_said_after_the_end_is_one_last_reply(
    stored: AsyncMock,
) -> None:
    """Armed on the first, the call would end before the second was voiced."""
    seen = await _run(
        BackendOutput(text="Your table is booked for 8pm."),
        BackendOutput(text="Goodbye!"),
        BackendIdle(),
    )

    assert seen == [
        "arm when quiet",
        (_COMMENTARY, "Your table is booked for 8pm.\n\nGoodbye!"),
    ]


@pytest.mark.parametrize(
    ("reported", "before", "outcome"),
    [
        # It ended the call, which is what it was asked: #247's `failed`
        # reads an empty answer as a delegation that did nothing.
        ((BackendIdle(),), [(_THINKING, NO_OUTPUT)], "answered"),
        (
            # Said goodbye beside the tool call, then nothing: a note, unspoken.
            (BackendOutput(text="Goodbye!", prefers_spoken=False), BackendIdle()),
            [(_THINKING, "Goodbye!"), (_THINKING, NO_OUTPUT)],
            "answered",
        ),
        ((BackendError(error="boom"),), [(_THINKING, NOT_COMPLETED)], "failed"),
    ],
)
async def test_with_nothing_to_voice_the_call_ends_after_the_reply_playing(
    stored: AsyncMock, reported: Any, before: list[Any], outcome: str
) -> None:
    """pipecat's stand-in sentence is not the caller's last words."""
    seen = await _run(*reported)

    assert seen == [*before, ("arm", True)]
    assert [p["outcome"] for p in _payloads(stored)] == [outcome]


async def test_a_call_not_ending_is_relayed_as_ever(stored: AsyncMock) -> None:
    seen = await _run(BackendIdle(), ending=False)

    assert seen == [(_COMMENTARY, NO_OUTPUT)]
    assert [p["outcome"] for p in _payloads(stored)] == ["failed"]


async def test_a_goodbye_the_delegate_does_not_finish_is_said_anyway(
    stored: AsyncMock,
) -> None:
    """Still working after its goodbye — a slow run, a stuck tool — the
    delegate does not hold the caller in silence: the goodbye is said once the
    hold is up, and what it says after that is kept from the caller."""
    with patch.object(delegation_events, "_GOODBYE_HOLD_SECS", 0):
        seen = await _run(
            BackendOutput(text="Goodbye!"),
            "wait",
            BackendOutput(text="The booking is confirmed."),
            BackendIdle(),
        )

    assert seen == [
        "arm when quiet",
        (_COMMENTARY, "Goodbye!"),
        (_THINKING, "The booking is confirmed."),
    ]
    assert [p["outcome"] for p in _payloads(stored)] == ["answered"]


async def test_a_delegation_that_started_after_the_end_did_not_end_the_call(
    stored: AsyncMock,
) -> None:
    """Waiting behind the one that ended the call, it is relayed as any other
    and answered only if it said something: no second goodbye, no second arm."""
    silent = await _run(BackendIdle(), ended_before=True)
    assert silent == [(_COMMENTARY, NO_OUTPUT)]

    spoke = await _run(BackendOutput(text="Noted."), BackendIdle(), ended_before=True)
    assert spoke == [(_COMMENTARY, "Noted.")]
    assert [p["outcome"] for p in _payloads(stored)] == ["failed", "answered"]
