"""A call must actually end at max_call_duration_seconds.

The field was accepted, validated 60-14400, stored and read by nothing. A call
that hung ran until the carrier or an idle timeout stopped it, billed the whole
way — the one gap on the inert list that cost money rather than credibility.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from turncall.domain.call_state import infer_ended_reason
from turncall.domain.enums import CallEventType, CallStatus, EndedReason
from turncall.orchestrator.call_session import CallSession


def _session(budget: int | None) -> CallSession:
    ctx = SimpleNamespace(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_factory=AsyncMock(),
        mcp_manager=None,
    )
    return CallSession(
        call_context=ctx,  # type: ignore[arg-type]
        transport=SimpleNamespace(),
        pipeline=SimpleNamespace(),  # type: ignore[arg-type]
        max_call_duration_seconds=budget,
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheGuard:
    async def test_it_cancels_the_worker_when_the_budget_runs_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _session(budget=1)
        worker = AsyncMock()
        session._task = worker
        logged: list[tuple] = []

        async def capture(event_type, payload):
            logged.append((event_type, payload))

        monkeypatch.setattr(session, "_log_event", capture)
        # Collapse the wait rather than actually sleeping a second.
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        guard = session._start_duration_guard()
        assert guard is not None
        await guard

        worker.cancel.assert_awaited_once()
        assert logged == [
            (CallEventType.CALL_MAX_DURATION_REACHED, {"max_call_duration_seconds": 1})
        ]

    async def test_the_event_is_recorded_before_the_hangup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_finalize_call derives ended_reason from the event log, so an event
        written after the cancel would arrive too late to be read."""
        session = _session(budget=1)
        order: list[str] = []

        worker = SimpleNamespace()

        async def cancel():
            order.append("cancel")

        worker.cancel = cancel
        session._task = worker  # type: ignore[assignment]

        async def log(event_type, payload):
            order.append("event")

        monkeypatch.setattr(session, "_log_event", log)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await session._start_duration_guard()

        assert order == ["event", "cancel"]

    async def test_no_budget_means_no_guard(self) -> None:
        assert _session(budget=None)._start_duration_guard() is None
        assert _session(budget=0)._start_duration_guard() is None


@pytest.mark.unit
class TestTheReasonIsHonest:
    def test_a_capped_call_is_not_reported_as_a_customer_hangup(self) -> None:
        """Without a branch of its own the cap landed on COMPLETED, which reads
        back as `customer_ended_call` — the one thing it certainly was not."""
        reason = infer_ended_reason(
            CallStatus.COMPLETED,
            {"call.started", "call.max_duration_reached"},
            assistant_ended=False,
        )

        assert reason == EndedReason.MAX_DURATION_REACHED

    def test_a_transfer_still_wins(self) -> None:
        """Transfer says what became of the call; the call left by transfer
        even if the cap fired on the way out."""
        reason = infer_ended_reason(
            CallStatus.COMPLETED,
            {"call.transferred", "call.max_duration_reached"},
            assistant_ended=False,
        )

        assert reason == EndedReason.TRANSFERRED

    def test_an_ordinary_hangup_is_unaffected(self) -> None:
        reason = infer_ended_reason(
            CallStatus.COMPLETED, {"call.started"}, assistant_ended=False
        )

        assert reason == EndedReason.CUSTOMER_ENDED_CALL
