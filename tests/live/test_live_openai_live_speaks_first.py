"""gpt-live-1 speaks first, over the eval loopback (#248).

The caller says nothing: the first turn has no `user:`, so the only way it
passes is the agent opening the call by itself. The opening now reaches
gpt-live-1 as a trailing developer message, which pipecat delivers as
commentary once the session starts. Measured before it, an agent with no
`first_message` sat silent for 30s in 5 of 5 runs; one with a
`first_message` greeted from its instructions alone in 15 of 15 here, though
#243's eval saw it stay silent. With it: 15/15 and 10/10, the greeting
starting ~0.8s after the session does (from ~1.6s). Several runs per config,
because a model that greets "sometimes" is the bug.

Audio modality: gpt-live-1 is heard, not read (see
`test_live_openai_live_delegation.py`).

Live: needs OPENAI_API_KEY. Skipped without it.
"""

import os
from typing import Any
from uuid import uuid4

import pytest

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_RUNS = 5


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


@pytest.mark.parametrize(
    "first_message",
    ["Thanks for calling Luigi's, how can I help?", None],
    ids=["first_message", "no_first_message"],
)
async def test_the_agent_opens_the_call_unprompted(
    session_factory: Any, first_message: str | None
) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("needs OPENAI_API_KEY")
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are the receptionist at Luigi's restaurant.",
        "first_message": first_message,
        "s2s": {"provider": "openai_live"},
    }
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    definition = {
        "turns": [
            # "help", not the greeting's own words: the opening reaches the
            # model as commentary, which it paraphrases — measured, it once
            # said only "How can I help?" in 15 runs. Silence is the bug.
            {
                "expect": [
                    {"event": "response", "text_contains": "help", "within_ms": 30000}
                ]
            },
        ]
    }
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.AUDIO),
        name="live-openai-live-speaks-first",
    )

    failures = []
    for _ in range(_RUNS):
        result = await run_iteration(
            parsed=parsed,
            kind=EvalKind.SCRIPTED,
            target=target,
            modality=EvalModality.AUDIO,
            settings=Settings(),
            session_factory=session_factory,
            run_id=uuid4(),
            tool_mocks=None,
        )
        if not result.passed:
            failures.append([str(f) for f in result.failures])
    assert not failures, f"{len(failures)}/{_RUNS} runs did not greet: {failures}"
