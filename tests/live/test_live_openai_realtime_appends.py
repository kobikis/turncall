"""A typed caller turn reaches an OpenAI Realtime session (#390).

An eval's typed caller turn is an append queued at the pipeline's source, as
the S2S idle check-in is. pipecat's OpenAI Realtime service, like Grok's,
sent nothing for one until `realtime_appends` carried it around the user
aggregator, so the second turn below went unanswered.

Live: needs OPENAI_API_KEY. Skipped without it.
"""

from typing import Any
from uuid import uuid4

import pytest

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.services.tool_mocks import ToolMocks

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_a_typed_caller_turn_is_answered(
    openai_key: str, session_factory: Any
) -> None:
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are the receptionist at Luigi's restaurant. "
        "Keep every reply to one short sentence.",
        "first_message": "Thanks for calling Luigi's, how can I help?",
        "s2s": {"provider": "openai"},
    }
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    definition = {
        "turns": [
            {
                "expect": [
                    {
                        "event": "llm_response",
                        "text_contains": "Luigi",
                        "within_ms": 20000,
                    }
                ]
            },
            {
                "user": "What is two plus two? Answer with the number as a word.",
                "expect": [
                    {
                        "event": "llm_response",
                        "text_contains": "four",
                        "within_ms": 20000,
                    }
                ],
            },
        ]
    }
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.TEXT),
        name="live-openai-realtime-typed-turn",
    )
    agent_errors: list[str] = []

    result = await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.TEXT,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=ToolMocks(),
        agent_errors=agent_errors,
    )

    assert result.passed, ([str(f) for f in result.failures], agent_errors)
