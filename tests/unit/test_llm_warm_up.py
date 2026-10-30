"""`llm.warm_up`: the first reply does not pay for a cold LLM client."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator.llm_warm_up import warm_up, warm_up_on_first_turn
from turncall.orchestrator.pipeline_factory import (
    _create_llm_service,
    _create_llm_stage,
)
from turncall.orchestrator.procedures import CallProcedures

pytestmark = pytest.mark.unit


def _llm(**over: object):
    """The real OpenAI service TurnCall builds, its request mocked."""
    config = AgentConfig(llm=LLMConfig(model="gpt-4.1-mini", **over))
    llm = _create_llm_service(config, creds("sk-test"), system_instruction="Be Maya.")
    create = AsyncMock()
    llm._client.chat.completions.create = create
    return llm, create


async def test_the_first_turn_warms_the_llm_once() -> None:
    llm, create = _llm()
    user = LLMUserAggregator(LLMContext())
    warm_up_on_first_turn(user, llm)

    for _ in range(2):
        await user._call_event_handler("on_user_turn_started", None)
        await asyncio.sleep(0.01)

    create.assert_awaited_once()
    sent = create.call_args.kwargs
    assert sent["model"] == "gpt-4.1-mini"
    assert sent["messages"][0] == {"role": "system", "content": "Be Maya."}
    assert sent["max_completion_tokens"] == 1


async def test_a_failover_group_warms_its_live_member() -> None:
    config = AgentConfig(
        llm=LLMConfig(model="gpt-4.1-mini", failover=[LLMConfig(model="gpt-4o-mini")])
    )
    group = _create_llm_stage(config, creds("sk-test"))
    create = AsyncMock()
    group.active_llm._client.chat.completions.create = create

    await warm_up(group)

    assert create.call_args.kwargs["model"] == "gpt-4.1-mini"


async def test_a_failed_warm_up_is_swallowed() -> None:
    llm, create = _llm()
    create.side_effect = RuntimeError("403 restricted key")

    await warm_up(llm)


async def test_a_service_with_no_openai_client_is_skipped() -> None:
    await warm_up(object())


def test_the_flag_survives_ingest() -> None:
    blob = {"llm": {"warm_up": True}}
    config = AgentConfig.model_validate(
        AgentConfigSchema.model_validate(blob).model_dump()
    )

    assert config.llm.warm_up
    assert not AgentConfig().llm.warm_up


@pytest.mark.parametrize("on", [True, False])
def test_create_pipeline_arms_it_only_when_asked(mocker, on: bool) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from turncall.config.settings import PipecatSettings
    from turncall.orchestrator import pipeline_factory

    built: list = []
    mocker.patch.object(
        pipeline_factory, "Pipeline", lambda ps: built.extend(ps) or mocker.MagicMock()
    )
    pipeline_factory.create_pipeline(
        config=AgentConfig(llm=LLMConfig(warm_up=on)),
        transport=mocker.MagicMock(),
        call_context=SimpleNamespace(
            is_eval=False,
            call_id=uuid4(),
            session_factory=None,
            procedures=CallProcedures(),
        ),
        credentials=creds("sk-test", deepgram="dg-test"),
        pipecat_settings=PipecatSettings(),
    )
    user = next(p for p in built if isinstance(p, LLMUserAggregator))

    assert len(user._event_handlers["on_user_turn_started"].handlers) == int(on)
