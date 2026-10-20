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
`_finish_delegation`, with `_current_delegation` still naming it — that is the
one place read. `answered` is pipecat's own flag (the delegate said something
for the caller to hear; its notes do not count); `timed_out` is the one end
that reaches `_finish_delegation` with the timeout task already cleared; an
error is caught on its way to `push_error`, by the delegation id pipecat puts
in its message. The duration runs from creation, so the wait behind an earlier
delegation counts; a delegation the call's end drops (`_abandon_delegations`)
records nothing.

Responses mode ends a delegation at the first terminal response pipecat will
not continue — read from its own pending-response state, so a function call it
refused cannot hold one open. It has no delegation timeout of its own — #243
bounds each function call instead, and the delegate answers past an expired
one — so `timed_out` is client mode's only. `test_delegation_completed.py` pins
the private surface.

A client delegation that ends the call (#262): gpt-live-1 is told to leave
the goodbye to it (`s2s_config`), and may still fill the wait ("One moment").
So the delegate's tool only marks the call as ending, and what the delegate
says for the caller after that is held until the delegation finishes — the
delegate's output is a stream, and it can say more than one thing — then
relayed as one, with the gate armed once gpt-live-1 is quiet: the goodbye it
voices is a reply of its own and the one the call ends after. With nothing of
the delegate's to relay, the call ends after the reply playing: pipecat's
stand-in sentences go as thinking, not as the call's last words.
`test_client_delegate_one_goodbye.py`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pipecat.services.openai.live import events
from pipecat.services.openai.live.llm import ClientDelegation, OpenAILiveLLMService
from pipecat.utils.errors import ErrorCategory

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


@dataclass
class _ClientDelegation:
    """A client delegation from creation to its end, by pipecat's id."""

    created: float
    failed: bool = False
    # Said for the caller after the call was marked ending, held for the end.
    goodbye: list[str] = field(default_factory=list)


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
        # Responses delegations in flight, by id: when each started.
        self._responses_started: dict[str, float] = {}

    @property
    def delegate_llm(self) -> Any:
        """The client delegate's LLM (a failover group, #246), or None."""
        # Only a ClientDelegation has a backend; responses mode has none.
        return getattr(getattr(self._delegation, "backend", None), "llm", None)

    async def _completed(self, mode: str, started: float, outcome: str) -> None:
        payload = delegation_payload(mode, time.monotonic() - started, outcome)
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

    async def _finish_delegation(self) -> None:
        current = self._current_delegation
        tracked = self._client.pop(current.id, None) if current is not None else None
        if current is None or tracked is None:
            await super()._finish_delegation()
            return
        # pipecat clears the timeout task before it finishes a timed-out
        # delegation; every other end still holds it.
        timed_out = current.timeout_task is None
        if not timed_out and current.timeout_task is not asyncio.current_task():
            # Ended: nothing below may race its timeout.
            await self.cancel_task(current.timeout_task)
            current.timeout_task = None
        gate = self._ending_gate()
        if timed_out:
            outcome = "timed_out"
        elif tracked.failed:
            outcome = "failed"
        else:
            # A delegation that ended the call did what it was asked, spoken
            # or not; any other is answered only if the caller heard from it.
            answered = current.answered or gate is not None
            outcome = "answered" if answered else "failed"
        if gate is not None and tracked.goodbye:
            await gate.arm_when_quiet()
            await super()._send_context_append(
                current.id, "\n\n".join(tracked.goodbye), spoken=True
            )
        elif gate is not None:
            # Nothing for gpt-live-1 to voice: end after what it is saying.
            await gate.queue_frame(HangupArmFrame(current_reply=True))
        await self._completed("client", tracked.created, outcome)
        await super()._finish_delegation()

    async def _abandon_delegations(self) -> None:
        # The session is over: what was running or waiting did not end, the
        # call did, so none of it has an outcome.
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
        if tracked is not None and self._ending_gate() is not None:
            if text in (NO_OUTPUT, NOT_COMPLETED):
                # A stand-in is not a goodbye: kept from the caller.
                spoken = False
            elif spoken:
                tracked.goodbye.append(text)
                return
        await super()._send_context_append(delegation_id, text, spoken=spoken)

    async def push_error(
        self,
        error_msg: str,
        exception: Exception | None = None,
        fatal: bool = False,
        category: ErrorCategory | None = None,
        force_treat_as_permanent: bool = False,
    ) -> None:
        current = self._current_delegation
        if current is not None and error_msg.startswith(
            f"Delegation {current.id} failed"
        ):
            tracked = self._client.get(current.id)
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
