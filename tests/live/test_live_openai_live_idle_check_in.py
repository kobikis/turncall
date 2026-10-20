"""gpt-live-1 checks in on a quiet caller, over the eval loopback (#257).

A short `user_idle_timeout_ms` keeps each run brief. The scenario asserts only
that a reply arrived (in audio mode a `response` is audio the agent actually
produced); what it said is checked below, since gpt-live-1 words it.

Audio modality: gpt-live-1 is heard, not read (see
`test_live_openai_live_delegation.py`).

Live: needs OPENAI_API_KEY. Skipped without it.
"""

import asyncio
import re
from typing import Any
from uuid import uuid4

import pytest
from pipecat.workers.llm.backend_llm_worker import _BackendSession

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.services.tool_mocks import ToolMocks

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_REPLY = {"expect": [{"event": "response", "within_ms": 30000}]}
_EMPTY = {"type": "object", "properties": {}}
_AGENT = {
    "pipeline_mode": "s2s",
    "system_prompt": "You are the receptionist at Luigi's restaurant.",
    "first_message": "Thanks for calling Luigi's, how can I help?",
    "user_idle_timeout_ms": 3000,
    "s2s": {"provider": "openai_live"},
}


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def _replies(
    blob: dict[str, Any],
    turns: list[dict[str, Any]],
    session_factory: Any,
    tool_mocks: ToolMocks | None = None,
) -> list[str]:
    parsed = scenario_mod.parse(
        scenario_mod.with_modality({"turns": turns}, EvalModality.AUDIO),
        name="live-openai-live-idle-check-in",
    )
    result = await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=ResolvedTarget(
            project_id=uuid4(),
            config=AgentConfig.model_validate(blob),
            config_blob=blob,
            agent_id=None,
        ),
        modality=EvalModality.AUDIO,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=tool_mocks,
    )
    return [e["text"] for e in result.events_seen if e["type"] == "response"]


def _asks(reply: str) -> bool:
    # "You still there?", "Hello, are you there?"
    return re.search(r"you (still )?there", reply.lower()) is not None


async def test_a_quiet_caller_is_asked_whether_they_are_still_there(
    openai_key: str, session_factory: Any
) -> None:
    """The greeting, then nothing from the caller. Measured before the fix:
    5/5 silent for 30s — and no second strike either, since the timer restarts
    only when the agent stops speaking. After: 5/5 asked, and the second
    strike then ended the call as `customer_silent`."""
    runs = [await _replies(_AGENT, [_REPLY, _REPLY], session_factory) for _ in range(5)]
    # Not always the second reply: a greeting can arrive as two.
    missed = [replies for replies in runs if not any(map(_asks, replies[1:]))]
    assert not missed, missed


async def test_a_caller_waiting_on_the_delegate_is_not_asked(
    openai_key: str, session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gpt-live-1 says "one moment" and goes quiet while its delegate works,
    and the idle timer runs. A delegate slower than the timeout: measured
    without the guard, 3/3 were asked "are you still there?" mid-delegation
    and hung up on the second strike before the answer arrived. With it,
    3/3 heard the hours first."""
    # Each delegation is put to the delegate over its session (pipecat 1.13):
    # held there, the delegation is in flight and the delegate slow.
    real = _BackendSession.send

    async def _slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(9)
        return await real(*args, **kwargs)

    monkeypatch.setattr(_BackendSession, "send", _slow)
    blob = {
        **_AGENT,
        "tools": [
            {
                "name": "check_hours",
                "description": "Look up the restaurant's opening hours.",
                "parameters_schema": _EMPTY,
                "webhook_url": "https://example.invalid/hours",
            }
        ],
    }
    turns = [_REPLY, {"user": "Hi, what time do you open today?", **_REPLY}, _REPLY]
    for _ in range(3):
        mocks = ToolMocks(responses={"check_hours": {"today": "12:00-22:00"}})
        replies = await _replies(blob, turns, session_factory, mocks)
        answer = next(
            (
                i
                for i, r in enumerate(replies)
                if re.search(r"12|noon|twelve", r.lower())
            ),
            None,
        )
        assert answer is not None, replies
        assert not any(_asks(r) for r in replies[:answer]), replies
