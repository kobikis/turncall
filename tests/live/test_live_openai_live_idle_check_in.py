"""gpt-live-1 checks in on a quiet caller, over the eval loopback (#257).

Two bot-only turns: the greeting, then nothing from the caller. The second
turn passes only if the idle guard's first strike makes gpt-live-1 speak.
Measured before the fix: 5/5 silent for 30s — and no second strike either,
since the timer restarts only when the agent stops speaking. After: 5/5
("You still there?", "Hello, are you still there?"), and the second strike
then ended the call as `customer_silent`. A short `user_idle_timeout_ms`
keeps each run brief.

Audio modality: gpt-live-1 is heard, not read (see
`test_live_openai_live_delegation.py`).

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

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_RUNS = 5
# What a check-in says, however gpt-live-1 words it.
_CHECK_IN_WORDS = ("there", "still", "hear", "with me", "hello")


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_a_quiet_caller_is_asked_whether_they_are_still_there(
    openai_key: str, session_factory: Any
) -> None:
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are the receptionist at Luigi's restaurant.",
        "first_message": "Thanks for calling Luigi's, how can I help?",
        "user_idle_timeout_ms": 3000,
        "s2s": {"provider": "openai_live"},
    }
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    turn = {"expect": [{"event": "response", "within_ms": 30000}]}
    parsed = scenario_mod.parse(
        scenario_mod.with_modality({"turns": [turn, turn]}, EvalModality.AUDIO),
        name="live-openai-live-idle-check-in",
    )

    failed, check_ins = [], []
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
            failed.append([str(f) for f in result.failures])
        responses = [e["text"] for e in result.events_seen if e["type"] == "response"]
        check_ins += responses[1:2]
    assert not failed, f"{len(failed)}/{_RUNS} runs never checked in: {failed}"
    # The second response is the check-in, not the greeting run long.
    asked = [c for c in check_ins if any(w in c.lower() for w in _CHECK_IN_WORDS)]
    assert len(asked) == _RUNS, check_ins
