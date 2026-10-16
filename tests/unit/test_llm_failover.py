"""LLM failover (#222): backup providers a call moves to when its LLM dies."""

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMContextFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import run_test
from pipecat.utils.errors import ErrorCategory
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema, LLMConfigSchema
from turncall.domain.models import AgentConfig
from turncall.orchestrator.llm_failover import FailoverLLM
from turncall.orchestrator.pipeline_factory import _create_llm_stage
from turncall.services.call_view import CallFacts, derived_fields


class _FakeLLM(FrameProcessor):
    """Answers a context with `reply`, or fails it with `fail` (a category)."""

    def __init__(self, reply: str, fail: ErrorCategory | None = None) -> None:
        super().__init__()
        self.reply = reply
        self.fail = fail
        self.contexts = 0

    def _sync_registered_tool_handlers(self, tools: object) -> None:
        pass

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        self.contexts += 1
        if self.fail is not None:
            await self.push_error(f"{self.reply} broke", category=self.fail)
            return
        await self.push_frame(LLMTextFrame(text=self.reply))


@pytest.mark.unit
class TestSwitching:
    async def test_unusable_error_switches_and_answers_the_same_turn(self) -> None:
        primary = _FakeLLM("primary", fail=ErrorCategory.AUTHENTICATION)
        backup = _FakeLLM("backup")
        switcher = FailoverLLM([("openai/gpt-x", primary), ("anthropic/c", backup)])
        seen: list[tuple[str, str, str]] = []

        @switcher.event_handler("on_failover")
        async def _on(_s: object, src: str, dst: str, error: ErrorFrame) -> None:
            seen.append((src, dst, error.error))

        down, up = await run_test(
            switcher,
            frames_to_send=[LLMContextFrame(context=LLMContext(messages=[]))],
        )

        assert [f.text for f in down if isinstance(f, LLMTextFrame)] == ["backup"]
        assert seen == [("openai/gpt-x", "anthropic/c", "primary broke")]
        # The switch absorbed the error: nothing upstream ends the call.
        assert not [f for f in up if isinstance(f, ErrorFrame)]

    async def test_recoverable_error_does_not_switch(self) -> None:
        primary = _FakeLLM("primary", fail=ErrorCategory.RATE_LIMIT)
        backup = _FakeLLM("backup")
        switcher = FailoverLLM([("a/1", primary), ("b/2", backup)])

        _down, up = await run_test(
            switcher,
            frames_to_send=[LLMContextFrame(context=LLMContext(messages=[]))],
        )

        assert backup.contexts == 0
        assert switcher.active_llm is primary
        assert [f for f in up if isinstance(f, ErrorFrame)]

    async def test_no_switching_back(self) -> None:
        primary = _FakeLLM("primary", fail=ErrorCategory.AUTHENTICATION)
        backup = _FakeLLM("backup", fail=ErrorCategory.AUTHENTICATION)
        switcher = FailoverLLM([("a/1", primary), ("b/2", backup)])

        await run_test(
            switcher,
            frames_to_send=[LLMContextFrame(context=LLMContext(messages=[]))],
        )

        # Both dead: the primary is never handed the turn a second time.
        assert primary.contexts == 1
        assert backup.contexts == 1

    def test_labels_name_the_service(self) -> None:
        a, b = _FakeLLM("a"), _FakeLLM("b")
        switcher = FailoverLLM([("openai/gpt-x", a), ("anthropic/c", b)])
        assert switcher.label(b) == "anthropic/c"
        assert FailoverLLM.group_of(b) is switcher
        lone = _FakeLLM("lone")
        assert FailoverLLM.group_of(lone) is lone


@pytest.mark.unit
class TestValidation:
    def test_s2s_agent_with_failover_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="S2S"):
            AgentConfigSchema(
                pipeline_mode="s2s",
                llm={"failover": [{"provider": "anthropic"}]},
            )

    def test_entry_is_validated_like_any_llm(self) -> None:
        with pytest.raises(ValidationError, match="model is required"):
            LLMConfigSchema(failover=[{"provider": "ollama"}])

    def test_entry_cannot_nest_its_own_failover(self) -> None:
        with pytest.raises(ValidationError, match="nest"):
            LLMConfigSchema(
                failover=[
                    {"provider": "anthropic", "failover": [{"provider": "openai"}]}
                ]
            )

    def test_fallback_models_unchanged(self) -> None:
        cfg = LLMConfigSchema(
            provider="openrouter", model="x/y", fallback_models=["z/w"]
        )
        assert cfg.fallback_models == ["z/w"]
        assert cfg.failover == []

    def test_survives_ingest_to_domain(self) -> None:
        schema = AgentConfigSchema(llm={"failover": [{"provider": "anthropic"}]})
        config = AgentConfig.model_validate(schema.model_dump())
        assert config.llm.failover[0].provider == "anthropic"


@pytest.mark.unit
class TestBuild:
    def test_no_failover_builds_a_plain_service(self) -> None:
        config = AgentConfig(system_prompt="hi")
        stage = _create_llm_stage(config, creds("sk-test"))
        assert not isinstance(stage, FailoverLLM)

    def test_failover_entries_resolve_their_own_keys(self) -> None:
        config = AgentConfig.model_validate(
            {"llm": {"failover": [{"provider": "anthropic"}]}}
        )
        stage = _create_llm_stage(config, creds("sk-test", anthropic="sk-ant"))
        assert isinstance(stage, FailoverLLM)
        assert [stage.label(s) for s in stage.llms] == [
            "openai/gpt-4o-mini",
            "anthropic/claude-sonnet-5",
        ]
        assert stage.llms[1]._client.api_key == "sk-ant"

    def test_missing_backup_key_is_reported(self) -> None:
        config = AgentConfig.model_validate(
            {"llm": {"failover": [{"provider": "anthropic"}]}}
        )
        missing = creds("sk-test", anthropic="").missing_keys(config)
        assert "anthropic" in missing


@pytest.mark.unit
def test_call_view_carries_failovers() -> None:
    call = SimpleNamespace(ended_at=object(), status="completed")
    record = {"role": "llm", "from": "a/1", "to": "b/2", "error": "401"}
    fields = derived_fields(call, CallFacts(failovers=(record,)))
    assert fields["failovers"] == [record]
    assert derived_fields(call, CallFacts())["failovers"] == []


@pytest.mark.unit
@pytest.mark.parametrize("is_eval", [False, True])
async def test_session_records_the_failover(is_eval: bool) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from pipecat.pipeline.pipeline import Pipeline

    from turncall.domain.enums import CallEventType
    from turncall.orchestrator.call_session import CallSession

    group = FailoverLLM([("a/1", _FakeLLM("a")), ("b/2", _FakeLLM("b"))])
    context = MagicMock(is_eval=is_eval, records=not is_eval, has_database=True)
    cs = CallSession(
        call_context=context, transport=MagicMock(), pipeline=Pipeline([group])
    )
    cs._watch_failover()

    with patch.object(cs, "_log_event", AsyncMock()) as log_event:
        await group._call_event_handler(
            "on_failover", "a/1", "b/2", ErrorFrame(error="401 bad key")
        )
        await asyncio.sleep(0.01)  # pipecat runs event handlers as tasks

    assert cs.pipeline_errors == ["llm failed over from a/1 to b/2: 401 bad key"]
    if is_eval:
        log_event.assert_not_awaited()
    else:
        log_event.assert_awaited_once_with(
            CallEventType.PROVIDER_FAILOVER,
            {"role": "llm", "from": "a/1", "to": "b/2", "error": "401 bad key"},
        )


@pytest.mark.unit
def test_failover_keys_are_masked_on_the_way_out() -> None:
    from turncall.domain.config_secrets import MASK, sanitize_config

    out = sanitize_config(
        {
            "llm": {
                "api_key": "sk-1",
                "failover": [{"api_key": "sk-2"}, {"api_key": None}],
            }
        }
    )
    assert out["llm"]["api_key"] == MASK
    assert out["llm"]["failover"] == [{"api_key": MASK}, {"api_key": None}]


@pytest.mark.unit
async def test_a_failed_llm_stays_failed_after_a_settings_update() -> None:
    """A handoff's prompt update reaches every member, and pipecat makes a
    service usable again on any settings change — which would let a second
    failover wrap round to the dead primary."""
    primary = _FakeLLM("primary", fail=ErrorCategory.AUTHENTICATION)
    switcher = FailoverLLM([("a/1", primary), ("b/2", _FakeLLM("b"))])
    await run_test(
        switcher, frames_to_send=[LLMContextFrame(context=LLMContext(messages=[]))]
    )
    assert not primary.is_usable

    await primary.set_usable(True)  # what a changed setting does
    await asyncio.sleep(0.01)  # pipecat runs event handlers as tasks

    assert not primary.is_usable


async def test_a_speculative_reply_is_held_and_released_through_the_group() -> None:
    """Flux's eager end of turn: the active member's gate holds the reply until
    the turn is confirmed, so the group must hand it both the speculative
    context and the confirming frame."""
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.services.llm_service import LLMService
    from pipecat.tests.utils import SleepFrame

    class _Speaks(LLMService):
        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                await self.push_frame(LLMFullResponseStartFrame())
                await self.push_frame(LLMTextFrame(text="Seven works."))
                await self.push_frame(LLMFullResponseEndFrame())
            else:
                await self.push_frame(frame, direction)

    async def run(frames: list) -> list[str]:
        group = FailoverLLM([("a/1", _Speaks()), ("b/2", _Speaks())])
        down, _ = await run_test(group, frames_to_send=frames)
        return [f.text for f in down if isinstance(f, LLMTextFrame)]

    guess = LLMContextFrame(context=LLMContext(messages=[]), speculation=True)
    assert await run([guess, SleepFrame(0.1)]) == []
    assert await run(
        [
            LLMContextFrame(context=LLMContext(messages=[]), speculation=True),
            SleepFrame(0.1),
            UserStoppedSpeakingFrame(),
            SleepFrame(0.1),
        ]
    ) == ["Seven works."]
