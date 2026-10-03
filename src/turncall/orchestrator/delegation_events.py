"""Delegation outcomes: one `delegation.completed` call event per delegation (#247).

gpt-live-1 decides by itself whether to delegate, and the known failure is it
acknowledging a request and never handing it off. pipecat says when a
delegation is *created* and nothing about how it ended, so the live service is
subclassed to tell: `{mode, duration_ms, outcome}`, outcome one of `answered`,
`timed_out`, `failed`, `declined`. Stored on the call's event log, never
dispatched to webhook subscribers (like `metrics.turn`), none in an eval.

Client mode reads pipecat's private delegation run — the backend's error is
swallowed there, so it is caught on its way to `push_error`, and a run with no
output is told apart by pipecat's own fallback sentence. Responses mode ends a
delegation at the first terminal response pipecat will not continue — read
from its own pending-response state, so a function call it refused cannot hold
one open. It has no delegation timeout of its own — #243 bounds
each function call instead, and the delegate answers past an expired one — so
`timed_out` is client mode's only. `test_delegation_completed.py` pins the
private surface.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pipecat.pipeline.job_context import JobError
from pipecat.services.openai.live import events
from pipecat.services.openai.live.llm import ClientDelegation, OpenAILiveLLMService
from pipecat.utils.errors import ErrorCategory

from turncall.domain.enums import CallEventType
from turncall.orchestrator.turn_metrics import store_call_event

if TYPE_CHECKING:
    from turncall.orchestrator.pipeline_factory import CallContext

# What pipecat appends for a client delegation that finished with no text.
NO_OUTPUT = "The delegated work finished without an answer."
# The reason pipecat's worker cancels a job that ran past its timeout with.
_TIMEOUT_REASON = "timeout"
_TERMINAL = ("response.completed", "response.failed", "response.incomplete")


@dataclass
class _Attempt:
    error: Exception | None = None
    answered: bool = False


# Each client delegation runs in a task of its own, so this is per delegation.
_attempt: ContextVar[_Attempt | None] = ContextVar("delegation_attempt", default=None)


def delegation_payload(mode: str, duration_secs: float, outcome: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "duration_ms": round(duration_secs * 1000),
        "outcome": outcome,
    }


def _client_outcome(attempt: _Attempt) -> str:
    if attempt.error is not None:
        timed_out = isinstance(attempt.error, JobError) and (
            str(attempt.error) == _TIMEOUT_REASON
        )
        return "timed_out" if timed_out else "failed"
    return "answered" if attempt.answered else "failed"


class DelegationTrackingLiveService(OpenAILiveLLMService):
    """gpt-live-1, reporting each delegation's end as `on_delegation_completed`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._register_event_handler("on_delegation_completed")
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
        if delegation.target == "responses":
            self._responses_started[delegation.id] = started
        await super()._handle_evt_delegation_created(evt)
        if delegation.target == "client" and not isinstance(
            self._delegation, ClientDelegation
        ):
            await self._completed("client", started, "declined")

    async def _run_client_delegation(
        self, delegation: events.DelegationMetadata
    ) -> None:
        attempt = _Attempt()
        _attempt.set(attempt)
        started = time.monotonic()
        # A cancellation (the call ending mid-delegation) propagates and
        # records nothing: the delegation did not end, the call did.
        await super()._run_client_delegation(delegation)
        await self._completed("client", started, _client_outcome(attempt))

    async def _send_context_append(
        self, delegation_id: str | None, text: str, *, spoken: bool
    ) -> None:
        attempt = _attempt.get()
        if attempt is not None and text != NO_OUTPUT:
            attempt.answered = True
        await super()._send_context_append(delegation_id, text, spoken=spoken)

    async def push_error(
        self,
        error_msg: str,
        exception: Exception | None = None,
        fatal: bool = False,
        category: ErrorCategory | None = None,
        force_treat_as_permanent: bool = False,
    ) -> None:
        attempt = _attempt.get()
        if attempt is not None and exception is not None:
            attempt.error = exception
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
    if call_context.is_eval or not isinstance(service, DelegationTrackingLiveService):
        return

    async def _store(_service: Any, payload: dict[str, Any]) -> None:
        await store_call_event(
            call_context, CallEventType.DELEGATION_COMPLETED, payload
        )

    service.add_event_handler("on_delegation_completed", _store)
