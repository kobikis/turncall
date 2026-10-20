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
    # A bare MagicMock makes every attribute truthy, which would read as an
    # eval and skip the call-scoped work this file is about.
    call_context.is_eval = False
    call_context.records = True

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
                "turncall.storage.repositories.call_repo.mark_call_ended",
                AsyncMock(return_value=call),
            ) as mark_ended,
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

            mark_ended.assert_awaited_once()
            kwargs = mark_ended.await_args.kwargs
            assert kwargs["status"] == "completed"
            assert kwargs["ended_at"] is not None
            assert kwargs["duration_ms"] is not None and kwargs["duration_ms"] > 0
            trigger.assert_called_once()

    async def test_a_call_already_ended_gets_post_call_only_if_unclaimed(
        self,
    ) -> None:
        """If /status or the end API ended it first, the guarded update ends
        nothing (#328), and post-call processing starts here only if they
        deferred it to the pipeline and nothing has started it (#356)."""
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
                "turncall.storage.repositories.call_repo.mark_call_ended",
                AsyncMock(return_value=None),
            ) as mark_ended,
            patch(
                "turncall.storage.repositories.call_repo.claim_post_call",
                AsyncMock(return_value=False),
            ) as claim,
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._finalize_call()

            mark_ended.assert_awaited_once()
            assert claim.await_args.kwargs["deferred"] is True
            trigger.assert_not_called()

    async def test_no_agent_and_no_inline_config_skips_analysis(self) -> None:
        call_id = uuid4()
        cs = _make_call_session(call_id)
        call = SimpleNamespace(
            id=call_id,
            project_id=uuid4(),
            status="in_progress",
            started_at=datetime.now(UTC),
            active_agent_id=None,
            metadata_json={},
        )

        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch(
                "turncall.storage.repositories.call_repo.mark_call_ended",
                AsyncMock(return_value=call),
            ) as mark_ended,
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._finalize_call()

            mark_ended.assert_awaited_once()
            trigger.assert_not_called()

    async def test_an_inline_agent_still_gets_post_call_processing(self) -> None:
        """A call whose agent came from call-init has no agent row. Skipping it
        here skips the call.ended webhook too, which is the event the whole
        post-call path — analysis, transcript, Automations — hangs off."""
        call_id = uuid4()
        cs = _make_call_session(call_id)
        inline = {"system_prompt": "hi", "analysis": {"enabled": True}}
        call = SimpleNamespace(
            id=call_id,
            project_id=uuid4(),
            status="in_progress",
            started_at=datetime.now(UTC),
            active_agent_id=None,
            metadata_json={"dynamic_config": inline},
        )

        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch(
                "turncall.storage.repositories.call_repo.mark_call_ended",
                AsyncMock(return_value=call),
            ),
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._finalize_call()

            trigger.assert_called_once()
            # It runs with the config the call actually ran with.
            assert trigger.call_args.args[3] == inline

    async def test_missing_call_is_noop(self) -> None:
        cs = _make_call_session(uuid4())
        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "turncall.storage.repositories.call_repo.mark_call_ended",
                AsyncMock(),
            ) as mark_ended,
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._finalize_call()

            mark_ended.assert_not_awaited()
            trigger.assert_not_called()


@pytest.mark.unit
class TestProviderError:
    """A provider pipecat stopped using ends the pipeline gracefully; the call
    must still read as failed, with the reason on its event log."""

    @staticmethod
    def _frame(*, usable: bool, fatal: bool = False):
        return SimpleNamespace(
            error="Error connecting: HTTP 401",
            fatal=fatal,
            processor=SimpleNamespace(is_usable=usable),
        )

    async def test_an_unusable_provider_fails_the_call(self) -> None:
        call_id = uuid4()
        cs = _make_call_session(call_id)
        call = SimpleNamespace(
            id=call_id,
            project_id=uuid4(),
            status="in_progress",
            started_at=datetime.now(UTC),
            active_agent_id=uuid4(),
        )
        with (
            patch.object(cs, "_log_event", AsyncMock()) as log_event,
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch(
                "turncall.storage.repositories.call_repo.mark_call_ended",
                AsyncMock(return_value=call),
            ) as mark_ended,
            patch(
                "turncall.services.call_analysis_trigger.config_for_call",
                AsyncMock(return_value={}),
            ),
            patch(
                "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
                MagicMock(),
            ) as trigger,
        ):
            await cs._on_pipeline_error(self._frame(usable=False))
            await cs._on_pipeline_error(self._frame(usable=False))
            await cs._finalize_call()

        log_event.assert_awaited_once()
        assert log_event.await_args.args[0] == "error.raised"
        assert "401" in log_event.await_args.args[1]["error"]
        assert mark_ended.await_args.kwargs["status"] == "failed"
        trigger.assert_called_once()  # call.ended still goes out

    async def test_a_recoverable_error_is_not_recorded(self) -> None:
        cs = _make_call_session(uuid4())
        with patch.object(cs, "_log_event", AsyncMock()) as log_event:
            await cs._on_pipeline_error(self._frame(usable=True))
        log_event.assert_not_awaited()


@pytest.mark.unit
class TestUnstartedCall:
    """A pipeline that never built: failed, with why, and call.ended."""

    @staticmethod
    def _ctx(*, is_eval: bool = False):
        return SimpleNamespace(
            call_id=uuid4(),
            project_id=uuid4(),
            session_factory=MagicMock(),
            is_eval=is_eval,
            records=not is_eval,
            has_database=True,
        )

    async def test_records_why_and_finalizes_as_failed(self) -> None:
        from turncall.orchestrator import call_session as mod

        ctx = self._ctx()
        with (
            patch.object(mod, "log_call_event", AsyncMock()) as log_event,
            patch.object(mod, "finalize_call", AsyncMock()) as finalize,
        ):
            await mod.fail_unstarted_call(ctx, RuntimeError("Missing credentials"))

        event_type, payload = log_event.await_args.args[1:]
        assert event_type == "error.raised"
        assert payload == {"stage": "pipeline_build", "error": "Missing credentials"}
        finalize.assert_awaited_once_with(ctx.session_factory, ctx.call_id, failed=True)

    async def test_an_eval_has_no_call_to_fail(self) -> None:
        from turncall.orchestrator import call_session as mod

        with patch.object(mod, "finalize_call", AsyncMock()) as finalize:
            await mod.fail_unstarted_call(
                self._ctx(is_eval=True),
                RuntimeError("x"),
            )
        finalize.assert_not_awaited()

    async def test_never_masks_the_original_error(self) -> None:
        from turncall.orchestrator import call_session as mod

        with (
            patch.object(mod, "log_call_event", AsyncMock()),
            patch.object(mod, "finalize_call", AsyncMock(side_effect=OSError("db"))),
        ):
            await mod.fail_unstarted_call(self._ctx(), RuntimeError("x"))


@pytest.mark.unit
def test_a_provider_error_keeps_the_reason_not_the_request() -> None:
    from turncall.orchestrator.call_session import readable_error

    raw = (
        "Deepgram rejected the connection (status 401): headers: {'User-Agent': "
        "'deepgram-sdk/7.11.0', 'Authorization': 'Token [REDACTED]'}, "
        "status_code: 401, body: Websocket initialized with invalid credentials."
    )
    assert readable_error(raw) == (
        "Deepgram rejected the connection (status 401): status_code: 401, "
        "body: Websocket initialized with invalid credentials."
    )
    assert len(readable_error("x" * 2000)) == 500


@pytest.mark.unit
async def test_a_rejected_key_names_its_provider() -> None:
    """A key that is present but wrong: pipecat says AUTHENTICATION; the call
    says which vendor's key to fix."""
    from pipecat.utils.errors import ErrorCategory

    cs = _make_call_session(uuid4())
    stt_class = type(
        "DeepgramSTTService",
        (),
        {"__module__": "pipecat.services.deepgram.stt", "is_usable": False},
    )
    processor = stt_class()
    frame = SimpleNamespace(
        error="Deepgram rejected the connection (status 401)",
        fatal=False,
        processor=processor,
        category=ErrorCategory.AUTHENTICATION,
    )
    with patch.object(cs, "_log_event", AsyncMock()) as log_event:
        await cs._on_pipeline_error(frame)
    payload = log_event.await_args.args[1]
    assert payload["error"] == (
        "The deepgram API key was rejected: "
        "Deepgram rejected the connection (status 401)"
    )
    assert (payload["provider"], payload["key_rejected"]) == ("deepgram", True)


@pytest.mark.unit
async def test_a_missing_key_is_named_on_the_event() -> None:
    from turncall.orchestrator import call_session as mod
    from turncall.services.credentials import MissingProviderKeyError

    ctx = SimpleNamespace(
        call_id=uuid4(),
        project_id=uuid4(),
        session_factory=MagicMock(),
        is_eval=False,
        records=True,
        has_database=True,
    )
    with (
        patch.object(mod, "log_call_event", AsyncMock()) as log_event,
        patch.object(mod, "finalize_call", AsyncMock()),
    ):
        await mod.fail_unstarted_call(ctx, MissingProviderKeyError(["deepgram"]))
    assert log_event.await_args.args[2]["missing_keys"] == ["deepgram"]


class TestProviderOf:
    """`error.raised` names the vendor whose key was refused off the service's
    class (#389): the nearest pipecat vendor class in its MRO."""

    def test_a_vendor_service_reports_its_vendor(self) -> None:
        from pipecat.services.deepgram.stt import DeepgramSTTService

        from turncall.orchestrator.call_session import provider_of

        assert provider_of(DeepgramSTTService.__new__(DeepgramSTTService)) == "deepgram"

    def test_a_turncall_subclass_of_a_vendor_service_reports_the_vendor(self) -> None:
        from pipecat.services.openai.llm import OpenAILLMService

        from turncall.orchestrator.call_session import provider_of

        class OurOpenAI(OpenAILLMService):
            pass

        assert OurOpenAI.__module__ == __name__
        assert provider_of(OurOpenAI.__new__(OurOpenAI)) == "openai"

    def test_a_vendor_neutral_base_reports_none(self) -> None:
        """`pipecat.services.llm_service` names a module, not a vendor."""
        from pipecat.services.llm_service import LLMService

        from turncall.orchestrator.call_session import provider_of

        class OurLLM(LLMService):
            pass

        assert provider_of(LLMService.__new__(LLMService)) is None
        assert provider_of(OurLLM.__new__(OurLLM)) is None

    def test_anything_outside_pipecat_services_reports_none(self) -> None:
        from turncall.orchestrator.call_session import provider_of

        assert provider_of(object()) is None
        assert provider_of(MagicMock()) is None
