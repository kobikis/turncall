"""Tests for CallSession._finalize_call — the transport-agnostic call end path.

This is the logic that finalizes a call on hangup for every transport (inbound
Twilio has no /status callback; WebRTC/WhatsApp have none at all). It must set
COMPLETED + ended_at + duration and fire post-call analysis, and be idempotent
vs the /status callback and the end_call tool.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.orchestrator.call_session import CallSession


def _make_call_session(call_id):
    """Build a CallSession with a mocked call_context (no DB, no pipeline)."""
    db_session = AsyncMock()  # AsyncSession; .commit() is awaited

    class _SessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_):
            return False

    call_context = MagicMock()
    call_context.call_id = call_id
    call_context.session_factory = lambda: _SessionCM()

    return CallSession(
        call_context=call_context,
        transport=MagicMock(),
        pipeline=MagicMock(),
    )


@pytest.mark.unit
class TestFinalizeCall:
    async def test_finalizes_in_progress_call(self) -> None:
        call_id = uuid4()
        cs = _make_call_session(call_id)
        call = SimpleNamespace(
            id=call_id,
            project_id=uuid4(),
            status="in_progress",
            started_at=datetime.now(UTC) - timedelta(seconds=5),
            active_agent_id=uuid4(),
        )
        agent = SimpleNamespace(config_blob={"system_prompt": "hi"})

        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch(
                "turncall.storage.repositories.call_repo.update_call_status",
                AsyncMock(),
            ) as update_status,
            patch(
                "turncall.storage.repositories.agent_repo.get_agent_by_id",
                AsyncMock(return_value=agent),
            ),
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._finalize_call()

            update_status.assert_awaited_once()
            kwargs = update_status.await_args.kwargs
            assert kwargs["status"] == "completed"
            assert kwargs["ended_at"] is not None
            assert kwargs["duration_ms"] is not None and kwargs["duration_ms"] > 0
            trigger.assert_called_once()

    async def test_skips_already_completed(self) -> None:
        """Idempotency: if /status or the end_call tool already finalized, do nothing."""
        call_id = uuid4()
        cs = _make_call_session(call_id)
        call = SimpleNamespace(
            id=call_id,
            project_id=uuid4(),
            status="completed",
            started_at=datetime.now(UTC),
            active_agent_id=uuid4(),
        )

        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch(
                "turncall.storage.repositories.call_repo.update_call_status",
                AsyncMock(),
            ) as update_status,
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._finalize_call()

            update_status.assert_not_awaited()
            trigger.assert_not_called()

    async def test_no_agent_skips_analysis(self) -> None:
        call_id = uuid4()
        cs = _make_call_session(call_id)
        call = SimpleNamespace(
            id=call_id,
            project_id=uuid4(),
            status="in_progress",
            started_at=datetime.now(UTC),
            active_agent_id=None,
        )

        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch(
                "turncall.storage.repositories.call_repo.update_call_status",
                AsyncMock(),
            ) as update_status,
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._finalize_call()

            update_status.assert_awaited_once()
            trigger.assert_not_called()

    async def test_missing_call_is_noop(self) -> None:
        cs = _make_call_session(uuid4())
        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "turncall.storage.repositories.call_repo.update_call_status",
                AsyncMock(),
            ) as update_status,
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._finalize_call()

            update_status.assert_not_awaited()
            trigger.assert_not_called()
