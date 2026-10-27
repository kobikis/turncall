"""Delegation outcomes: one `delegation.completed` call event per delegation (#247).

gpt-live-1 decides by itself whether to delegate, and the known failure is it
acknowledging a request and never handing it off. pipecat says when a
delegation is *created* and nothing about how it ended, so the live service is
subclassed to tell: `{mode, duration_ms, outcome}`, outcome one of `answered`,
`timed_out`, `failed`, `declined` (CONTEXT.md, "Delegation outcome"). Stored
on the call's event log, never dispatched to webhook subscribers (like
`metrics.turn`), none in an eval.

Client mode (pipecat 1.13, #367): the service holds one session with the
delegate and runs delegations one at a time, a later one waiting for the
delegate to go idle. Every delegation that ends, ends in pipecat's private
`_finish_delegation`, with `_current_delegation` still naming it — that is
where the outcome is read. `answered` is pipecat's own flag (the delegate said
something for the caller to hear; its notes do not count); `timed_out` is the
one end that reaches `_finish_delegation` with the timeout task already
cleared; `failed` is a `BackendError` (`_on_backend_event`) or a delegation
that could not be put to the delegate (`_start_delegation`). The duration runs
from creation to the end, so the wait behind an earlier delegation counts; a
delegation the call's end drops (`_abandon_delegations`) records nothing.

Responses mode ends a delegation at the first terminal response pipecat will
not continue — read from its own pending-response state, so a function call it
refused cannot hold one open. It has no delegation timeout of its own — #243
bounds each function call instead, and the delegate answers past an expired
one — so `timed_out` is client mode's only. `test_delegation_completed.py` pins
the private surface.

A client delegation that ends the call (#262): gpt-live-1 is told to leave
the goodbye to it (`s2s_config`), and may still fill the wait ("One moment").
So the delegate's tool only marks the call as ending, and what the delegate
says for the caller after that is held — its output is a stream, and a hangup
armed on the first message would cut off the next — until the delegation
finishes, or `_GOODBYE_HOLD_SECS` after the first if it is still working; then
relayed as one, with the gate armed once gpt-live-1 is quiet: the goodbye it
voices is a reply of its own and the one the call ends after. With nothing of
the delegate's to relay, the call ends after the reply playing: pipecat's
stand-in sentences go as thinking, not as the call's last words. A delegation
that started with the call already ending did not end it, and is relayed as
any other. gpt-live-1 still answers a caller's "bye" itself at times, against
its instructions (call f10f0cc0): when what it said since the caller last spoke
reads as a goodbye, the delegate's is dropped and the call ends after its own.
`test_client_delegate_one_goodbye.py`.

And having said its own goodbye, it sometimes never runs `end_call` at all
(#413, call 64c71433: the caller said bye during a booking, gpt-live-1 said
"Sure, goodbye." and the line stayed open for 35 s). So once the caller and
gpt-live-1 have both said goodbye and nothing is in flight, the call waits
`_UNENDED_GOODBYE_SECS` for an ending tool and then ends itself, after the reply
playing, recorded as the agent's end. Only for an agent that offers `end_call`.
`test_goodbye_without_end_call.py`.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.services.openai.live import events
from pipecat.services.openai.live.llm import ClientDelegation, OpenAILiveLLMService
from pipecat.utils.errors import ErrorCategory
from pipecat.workers.llm.backend_llm_worker import BackendError

from turncall.domain.enums import CallEventType
from turncall.orchestrator.hangup import HangupAfterReply, HangupArmFrame
from turncall.orchestrator.turn_metrics import store_call_event

if TYPE_CHECKING:
    from turncall.orchestrator.pipeline_factory import CallContext

# What pipecat appends for a client delegation that finished with nothing
# spoken, and for one that failed or timed out: its stand-ins, never a
# delegate's own words.
NO_OUTPUT = "The delegated work finished without an answer."
NOT_COMPLETED = "The delegated work could not be completed."
_TERMINAL = ("response.completed", "response.failed", "response.incomplete")
# How long the delegate's goodbye waits for it to finish before it is said
# anyway: past this the delegate is stuck, and the caller is in silence.
_GOODBYE_HOLD_SECS = 5.0
# gpt-live-1 saying goodbye in its own words. ponytail: English phrases only; a
# goodbye in another language is missed and the delegate's is said too, as
# before. Upgrade path: ask gpt-live-1 to mark it, or classify the turn.
# How long the call waits for an ending tool once the caller and gpt-live-1 have
# both said goodbye and nothing is in flight (#413). Measured live: when
# gpt-live-1 hands `end_call` over at all, the delegation is created within
# ~1-2 s of its reply (5/5 runs); one running or waiting holds the wait, and its
# end starts it again.
_UNENDED_GOODBYE_SECS = 4.0
# `call.ended`'s reason when the call ended itself on an unended goodbye.
GOODBYE_WITHOUT_END_CALL = "goodbye_without_end_call"
_FAREWELL = re.compile(
    r"\b(?:good)?bye\b|\bfarewell\b|\btake care\b"
    r"|\bhave a (?:\w+ )?(?:day|evening|night|weekend)\b",
    re.IGNORECASE,
)


@dataclass
class _ClientDelegation:
    """A client delegation from creation to its end, by pipecat's id."""

    created: float
    failed: bool = False
    # The call was already ending when it started, so it did not end it.
    after_the_end: bool = False
    # Said for the caller after the call was marked ending, held for the end.
    goodbye: list[str] = field(default_factory=list)
    goodbye_hold: asyncio.Task[None] | None = None
    goodbye_said: bool = False


def delegation_payload(mode: str, duration_secs: float, outcome: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "duration_ms": round(duration_secs * 1000),
        "outcome": outcome,
    }


class DelegationTrackingLiveService(OpenAILiveLLMService):
    """gpt-live-1, reporting each delegation's end as `on_delegation_completed`.

    `hangup_gate` is the call's, for a client delegation that ends it (#262).
    The idle guard reads `delegating` and speaks through `say` (#257).
    """

    def __init__(
        self, *args: Any, hangup_gate: HangupAfterReply | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._hangup_gate = hangup_gate
        self._register_event_handler("on_delegation_completed")
        # Client delegations not yet ended, running or waiting, by id.
        self._client: dict[str, _ClientDelegation] = {}
        # The delegation each task is starting (`_start_delegation`), by task.
        self._starting: dict[asyncio.Task[Any] | None, str] = {}
        # Responses delegations in flight, by id: when each started.
        self._responses_started: dict[str, float] = {}
        # What gpt-live-1 has said since the caller last started speaking.
        self._said_since_caller = ""
        # An unended goodbye (#413): what the caller is saying, whether their
        # last turn and gpt-live-1 since read as goodbyes, and the wait for an
        # ending tool. `_record_unended_goodbye` is set by
        # `end_unended_goodbyes`, for an agent that offers `end_call`.
        self._caller_turn = ""
        self._caller_said_bye = False
        self._agent_said_bye = False
        self._goodbye_wait: asyncio.Task[None] | None = None
        self._record_unended_goodbye: Callable[[], Awaitable[None]] | None = None

    @property
    def delegate_llm(self) -> Any:
        """The client delegate's LLM (a failover group, #246), or None."""
        # Only a ClientDelegation has a backend; responses mode has none.
        return getattr(getattr(self._delegation, "backend", None), "llm", None)

    async def _completed(
        self, mode: str, started: float, outcome: str, ended: float | None = None
    ) -> None:
        ended = time.monotonic() if ended is None else ended
        payload = delegation_payload(mode, ended - started, outcome)
        await self._call_event_handler("on_delegation_completed", payload)

    async def _handle_evt_delegation_created(
        self, evt: events.SessionDelegationCreatedEvent
    ) -> None:
        delegation = evt.delegation
        started = time.monotonic()
        client = isinstance(self._delegation, ClientDelegation)
        if delegation.target == "responses":
            self._responses_started[delegation.id] = started
        elif delegation.target == "client" and client:
            self._client[delegation.id] = _ClientDelegation(created=started)
        await super()._handle_evt_delegation_created(evt)
        if delegation.target == "client" and not client:
            await self._completed("client", started, "declined")

    async def _start_delegation(self, delegation: Any, request: str) -> None:
        tracked = self._client.get(delegation.id)
        if tracked is not None:
            tracked.after_the_end = self._ending_gate() is not None
        # Which delegation this task is starting, for a failure on the way.
        task = asyncio.current_task()
        outer = self._starting.get(task)
        self._starting[task] = delegation.id
        try:
            await super()._start_delegation(delegation, request)
        finally:
            if outer is None:
                self._starting.pop(task, None)
            else:
                self._starting[task] = outer

    async def _on_backend_event(self, event: Any) -> None:
        current = self._current_delegation
        if isinstance(event, BackendError) and current is not None:
            tracked = self._client.get(current.id)
            if tracked is not None:
                tracked.failed = True
        await super()._on_backend_event(event)

    async def _finish_delegation(self) -> None:
        current = self._current_delegation
        starting = self._starting.get(asyncio.current_task())
        if starting is not None and (current is None or current.id != starting):
            # A start failing after its delegation's timeout had already
            # finished it and started the next: pipecat would finish that one
            # too, with the delegate still working on it.
            return
        tracked = self._client.pop(current.id, None) if current is not None else None
        if current is None or tracked is None:
            await super()._finish_delegation()
            return
        ended = time.monotonic()
        # pipecat clears the timeout task before it finishes a timed-out
        # delegation; every other end still holds it.
        timed_out = current.timeout_task is None
        if not timed_out:
            # Ended: nothing below may race its timeout.
            await self.cancel_task(current.timeout_task)
            current.timeout_task = None
        gate = self._ending_gate()
        ended_the_call = gate is not None and not tracked.after_the_end
        if timed_out:
            outcome = "timed_out"
        elif tracked.failed:
            outcome = "failed"
        else:
            # One that ended the call did what it was asked, spoken or not;
            # any other is answered only if the caller heard from it.
            answered = current.answered or ended_the_call
            outcome = "answered" if answered else "failed"
        if ended_the_call:
            await self._say_goodbye(current.id, tracked)
            if not tracked.goodbye_said:
                # Nothing for gpt-live-1 to voice: end after what it is saying.
                await gate.queue_frame(HangupArmFrame(current_reply=True))
        await self._completed("client", tracked.created, outcome, ended)
        await super()._finish_delegation()
        await self._wait_for_end_call()

    async def _abandon_delegations(self) -> None:
        # The session is over: what was running or waiting did not end, the
        # call did, so none of it has an outcome.
        for tracked in self._client.values():
            if tracked.goodbye_hold is not None:
                await self.cancel_task(tracked.goodbye_hold)
        self._client.clear()
        await super()._abandon_delegations()

    @property
    def delegating(self) -> bool:
        """A delegation is running or waiting, so the caller is waiting on it (#257)."""
        return bool(self._client or self._responses_started)

    async def say(self, instruction: str) -> None:
        """Have gpt-live-1 speak now, in its own words (#257).

        A message appended to the context never reaches it — pipecat sets the
        session up from the first context and ignores every later one — so
        this is the channel #248's opening uses: spoken commentary.
        """
        await self._send_context_append(None, instruction, spoken=True)

    def _ending_gate(self) -> HangupAfterReply | None:
        """The call's gate, once a client delegate's tool has ended the call."""
        gate = self._hangup_gate
        return gate if gate is not None and gate.ending else None

    async def _send_context_append(
        self, delegation_id: str | None, text: str, *, spoken: bool
    ) -> None:
        tracked = self._client.get(delegation_id) if delegation_id else None
        if (
            tracked is not None
            and not tracked.after_the_end
            and self._ending_gate() is not None
        ):
            if text in (NO_OUTPUT, NOT_COMPLETED) or tracked.goodbye_said:
                # A stand-in is not a goodbye, and once one is said the call
                # ends after it: kept from the caller.
                spoken = False
            elif spoken:
                tracked.goodbye.append(text)
                if tracked.goodbye_hold is None:
                    tracked.goodbye_hold = self.create_task(
                        self._goodbye_after_hold(delegation_id, tracked),
                        f"{self}::goodbye_hold:{delegation_id}",
                    )
                return
        await super()._send_context_append(delegation_id, text, spoken=spoken)

    async def _goodbye_after_hold(
        self, delegation_id: str, tracked: _ClientDelegation
    ) -> None:
        await asyncio.sleep(_GOODBYE_HOLD_SECS)
        tracked.goodbye_hold = None
        await self._say_goodbye(delegation_id, tracked)

    async def _say_goodbye(
        self, delegation_id: str, tracked: _ClientDelegation
    ) -> None:
        """Relay what the delegate said after ending the call, as one reply the
        call ends after."""
        hold = tracked.goodbye_hold
        tracked.goodbye_hold = None
        if hold is not None and hold is not asyncio.current_task():
            await self.cancel_task(hold)
        gate = self._ending_gate()
        if gate is None or not tracked.goodbye:
            return
        if _FAREWELL.search(self._said_since_caller):
            # gpt-live-1 answered the caller's goodbye itself: a second one is
            # the double goodbye, so the call ends after its own.
            tracked.goodbye.clear()
            tracked.goodbye_said = True
            await gate.queue_frame(HangupArmFrame(current_reply=True))
            return
        text = "\n\n".join(tracked.goodbye)
        tracked.goodbye.clear()
        tracked.goodbye_said = True
        await gate.arm_when_quiet()
        await super()._send_context_append(delegation_id, text, spoken=True)

    async def _open_turn(self, role: str) -> None:
        if role == "user":
            self._said_since_caller = ""
            self._caller_turn = ""
            # The caller is speaking again: what they say decides.
            await self._stop_goodbye_wait()
        await super()._open_turn(role)

    async def _append_turn(
        self, role: str, delta: str, accumulated: str, evt: Any
    ) -> None:
        if role == "user":
            self._caller_turn = accumulated
        await super()._append_turn(role, delta, accumulated, evt)

    async def _push_assistant_text(self, text: str) -> None:
        self._said_since_caller += text
        if _FAREWELL.search(self._said_since_caller):
            self._agent_said_bye = True
        await super()._push_assistant_text(text)

    async def _end_turn(self, role: str) -> None:
        if role == "user" and self._user_turn.open:
            self._caller_said_bye = bool(_FAREWELL.search(self._caller_turn))
            if not self._caller_said_bye:
                # The conversation went on: an earlier goodbye no longer counts.
                self._agent_said_bye = False
        await super()._end_turn(role)
        await self._wait_for_end_call()

    async def _wait_for_end_call(self) -> None:
        """Both sides have said goodbye: (re)start the wait for an ending tool."""
        if self._record_unended_goodbye is None or not (
            self._caller_said_bye and self._agent_said_bye
        ):
            return
        await self._stop_goodbye_wait()
        self._goodbye_wait = self.create_task(
            self._end_if_unended(), f"{self}::unended_goodbye"
        )

    async def _end_if_unended(self) -> None:
        await asyncio.sleep(_UNENDED_GOODBYE_SECS)
        self._goodbye_wait = None
        gate = self._hangup_gate
        record = self._record_unended_goodbye
        # Ending already, or a delegation in flight — `end_call` itself, or
        # work whose relay comes first; its end starts the wait again.
        if gate is None or record is None or gate.ending or self.delegating:
            return
        logger.info("goodbye_without_end_call: ending the call")
        await record()
        await gate.queue_frame(HangupArmFrame(current_reply=True))

    async def _stop_goodbye_wait(self) -> None:
        task, self._goodbye_wait = self._goodbye_wait, None
        if task is not None and task is not asyncio.current_task():
            await self.cancel_task(task)

    async def cleanup(self) -> None:
        await self._stop_goodbye_wait()
        await super().cleanup()

    async def push_error(
        self,
        error_msg: str,
        exception: Exception | None = None,
        fatal: bool = False,
        category: ErrorCategory | None = None,
        force_treat_as_permanent: bool = False,
    ) -> None:
        # pipecat reports a delegation it could not put to the delegate here,
        # from within `_start_delegation`.
        starting = self._starting.get(asyncio.current_task())
        tracked = self._client.get(starting) if starting is not None else None
        if tracked is not None:
            tracked.failed = True
        await super().push_error(
            error_msg,
            exception,
            fatal=fatal,
            category=category,
            force_treat_as_permanent=force_treat_as_permanent,
        )

    async def _handle_evt_response(self, evt: events.ResponseEventEnvelope) -> None:
        await super()._handle_evt_response(evt)
        key = evt.delegation_id
        if key is None or key not in self._responses_started:
            return
        if evt.inner_type not in _TERMINAL:
            return
        # pipecat continues a response (failed or not) once every function
        # call it ran is answered, deleting its entry; one still awaiting an
        # answer is continued later. Only a response pipecat will not continue
        # ends the delegation.
        pending = self._pending_responses.get(key)
        if pending is None or pending.call_ids:
            return
        started = self._responses_started.pop(key)
        outcome = "answered" if evt.inner_type == "response.completed" else "failed"
        await self._completed("responses", started, outcome)


def record_delegations(service: Any, call_context: CallContext) -> None:
    """Store each delegation's outcome on a live call; nothing for an eval,
    which has no call row to hang them off (ADR-0018)."""
    if not call_context.records or not isinstance(
        service, DelegationTrackingLiveService
    ):
        return

    async def _store(_service: Any, payload: dict[str, Any]) -> None:
        await store_call_event(
            call_context, CallEventType.DELEGATION_COMPLETED, payload
        )

    service.add_event_handler("on_delegation_completed", _store)


def end_unended_goodbyes(
    service: Any, call_context: CallContext, tool_names: set[str]
) -> None:
    """Let an `openai_live` call end itself when the caller and gpt-live-1 have
    both said goodbye and no `end_call` follows (#413). Only for an agent that
    offers `end_call`, and only with a hangup gate (none in an eval)."""
    if (
        not isinstance(service, DelegationTrackingLiveService)
        or service._hangup_gate is None
        or "end_call" not in tool_names
    ):
        return

    async def _record() -> None:
        # Recorded before the hangup, as `end_call` is: the event log is what
        # `ended_reason` is read from.
        if not call_context.records:
            return
        from turncall.services import call_control

        async with call_context.session_factory() as session:
            await call_control.begin_assistant_end(
                session, call_context.call_id, reason=GOODBYE_WITHOUT_END_CALL
            )
            await session.commit()

    service._record_unended_goodbye = _record
