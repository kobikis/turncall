"""Tools on a Grok agent, against xAI itself (#388).

Grok calls the agent's tools from its own context, as OpenAI Realtime does, and
they run through TurnCall's tool bridge. Two seams:

- an eval in text modality, the tool mocked: Grok calls it, the bridge answers
  with the mock, and Grok speaks from that answer — a word it could only have
  from the result. That is also an eval run against an `xai` agent reaching a
  verdict. (Audio modality needs the local Kokoro voice; it was not installed
  where this was written, so audio is unmeasured.)
- the factory's pipeline on a call with no database (the website demo's
  shape: not an eval, so the hangup gate is in), driven by a typed caller
  line: `end_call` runs through the bridge, Grok says goodbye, and the gate
  ends the pipeline after that goodbye.

Both typed lines are appends queued at the pipeline's source, as the S2S idle
check-in is. Before `realtime_appends`, Grok never heard them and both tests
timed out in silence.

Live: needs XAI_API_KEY. Skipped without it.
"""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from tests.conftest import creds

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.services.tool_mocks import ToolMocks

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_EMPTY = {"type": "object", "properties": {}}
_PROMPT = (
    "You are the receptionist at Luigi's restaurant. Keep every reply to one "
    "short sentence."
)


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_a_mocked_tool_round_trips_and_the_eval_reaches_a_verdict(
    xai_key: str, session_factory: Any
) -> None:
    """Grok calls `check_specials`; TurnCall's bridge answers with the mock;
    Grok's next words carry what only the mock said ("saffron")."""
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": _PROMPT
        + " Always use check_specials to answer a question about specials.",
        "first_message": "Thanks for calling Luigi's, how can I help?",
        "s2s": {"provider": "xai"},
        "tools": [
            {
                "name": "check_specials",
                "description": "Look up tonight's specials.",
                "parameters_schema": _EMPTY,
                "webhook_url": "https://example.invalid/specials",
            }
        ],
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
                "user": "What's the special tonight?",
                "expect": [
                    {
                        "event": "function_call",
                        "calls": [{"name": "check_specials"}],
                        "within_ms": 30000,
                    },
                    {
                        "event": "llm_response",
                        "text_contains": "saffron",
                        "within_ms": 30000,
                    },
                ],
            },
        ]
    }
    mocks = ToolMocks(
        responses={"check_specials": {"special": "saffron risotto with prawns"}}
    )
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.TEXT),
        name="live-xai-tool-round-trip",
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
        tool_mocks=mocks,
        agent_errors=agent_errors,
    )

    assert [c["tool_name"] for c in mocks.calls] == ["check_specials"], (
        mocks.calls,
        agent_errors,
    )
    assert not mocks.refused
    assert result.passed, ([str(f) for f in result.failures], agent_errors)


class _Transport:
    """Passthrough input and output: the caller's line is queued as text,
    and Grok's audio leaves the pipeline unplayed."""

    def __init__(self) -> None:
        from pipecat.processors.filters.identity_filter import IdentityFilter

        self._input, self._output = IdentityFilter(), IdentityFilter()

    def input(self) -> Any:
        return self._input

    def output(self) -> Any:
        return self._output

    def event_handler(self, _name: str) -> Any:
        return lambda handler: handler


async def test_end_call_ends_the_call_after_the_goodbye(xai_key: str) -> None:
    from pipecat.frames.frames import (
        EndWorkerFrame,
        FunctionCallInProgressFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMMessagesAppendFrame,
        TextFrame,
    )
    from pipecat.observers.base_observer import BaseObserver, FramePushed
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService
    from pipecat.workers.runner import WorkerRunner

    from turncall.orchestrator.call_session import (
        run_until_pipeline_done,
        s2s_greeting_frame,
    )
    from turncall.orchestrator.hangup import HangupAfterReply, HangupArmFrame
    from turncall.orchestrator.pipeline_factory import CallContext, create_pipeline
    from turncall.orchestrator.tool_bridge import register_tools

    config = AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "system_prompt": _PROMPT
            + " When the caller says goodbye, say goodbye and call end_call.",
            "s2s": {"provider": "xai"},
            "tools": [
                {
                    "name": "end_call",
                    "description": "End the call once the caller has said goodbye.",
                    "parameters_schema": _EMPTY,
                }
            ],
        }
    )
    # No database: not an eval, so the hangup gate is built, and `end_call`
    # has no call row to record on (the website demo's call).
    call_context = CallContext(
        call_id=uuid4(),
        project_id=uuid4(),
        agent_id=uuid4(),
        call_sid="",
        stream_sid="",
        session_factory=None,
    )
    pipeline = create_pipeline(
        config=config,
        transport=_Transport(),
        call_context=call_context,
        credentials=creds(xai=xai_key),
        pipecat_settings=Settings().pipecat,
    )
    grok = next(p for p in pipeline.processors if isinstance(p, GrokRealtimeLLMService))
    gate = next(p for p in pipeline.processors if isinstance(p, HangupAfterReply))
    register_tools(grok, list(config.tools), call_context)

    seen: list[tuple[str, str]] = []  # (what, text) in the order pushed
    greeted = asyncio.Event()

    observed: set[int] = set()

    class _Watch(BaseObserver):
        async def on_push_frame(self, data: FramePushed) -> None:
            frame, source = data.frame, data.source
            if frame.id in observed:  # one push can be reported more than once
                return
            if source is grok or source is gate:
                observed.add(frame.id)
            if source is grok:
                if isinstance(frame, FunctionCallInProgressFrame):
                    seen.append(("call", frame.function_name))
                elif isinstance(frame, HangupArmFrame):
                    seen.append(("arm", ""))
                elif isinstance(frame, LLMFullResponseStartFrame):
                    seen.append(("start", ""))
                elif isinstance(frame, LLMFullResponseEndFrame):
                    seen.append(("end", ""))
                    greeted.set()
                elif isinstance(frame, TextFrame) and frame.text:
                    seen.append(("text", frame.text))
            elif (
                source is gate
                and isinstance(frame, EndWorkerFrame)
                and data.direction == FrameDirection.DOWNSTREAM
            ):
                seen.append(("hangup", ""))

    worker = PipelineWorker(pipeline, params=PipelineParams(), observers=[_Watch()])
    run = asyncio.create_task(run_until_pipeline_done(WorkerRunner(), worker))
    await worker.queue_frame(s2s_greeting_frame())  # as CallSession opens it
    try:
        await asyncio.wait_for(greeted.wait(), timeout=20)
        await worker.queue_frame(
            LLMMessagesAppendFrame(
                messages=[
                    {"role": "user", "content": "That's all, thank you. Goodbye!"}
                ],
                run_llm=True,
            )
        )
        # Nothing else is sent: the call ends only if the gate ends it.
        await asyncio.wait_for(run, timeout=45)
    finally:
        if not run.done():
            await worker.cancel()
            await asyncio.wait_for(run, timeout=10)

    kinds = [k for k, _ in seen]
    assert ("call", "end_call") in seen, f"{seen}"
    assert kinds.count("hangup") == 1 and kinds[-1] == "hangup", f"{seen}"
    # The gate was armed, then waited for a reply that started after it and
    # ended: it never hangs up on the arm itself. Grok often says goodbye in
    # the reply that calls end_call and answers the result with an empty one.
    after_arm = kinds[kinds.index("arm") :]
    assert after_arm[-2:] == ["end", "hangup"] and "start" in after_arm, f"{seen}"
    # The goodbye: words Grok said after the greeting, all before the hangup.
    greeting_end = kinds.index("end")
    goodbye = "".join(t for k, t in seen[greeting_end:] if k == "text").strip()
    assert goodbye, f"{seen}"
