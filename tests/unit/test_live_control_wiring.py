"""Every live call holds its control channel for exactly as long as it runs (#215).

The channel is how `POST /v1/calls/{id}/messages` and `/handoff` (#216) reach
a call. A transport that stopped subscribing would be silent about it — every
request to its calls a 409 that reads like a hangup — so the wiring is checked
structurally: the channel lives in `CallSession.start()`, and every voice
entry point ends there. The harness test (`test_harness_call_messages.py`)
proves the Twilio path end to end.
"""

import ast
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import turncall
from turncall.orchestrator import call_session as cs
from turncall.orchestrator import live_control
from turncall.orchestrator import transport_factory as tf
from turncall.services.live_call_channel import HANDOFF, MESSAGE, ControlRefused

pytestmark = pytest.mark.unit

VOICE_FACTORIES = {
    n
    for n, f in vars(tf).items()
    if n.startswith("create_")
    and n.endswith("_transport")
    and callable(f)
    and n != "create_eval_transport"
}


def _context(*, is_eval: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="CA1",
        session_factory=AsyncMock(),
        mcp_manager=None,
        is_eval=is_eval,
    )


class _Worker:
    """Just enough of a PipelineWorker for start() to run around it."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.queued: list[object] = []

    def event_handler(self, _name: str):
        return lambda fn: fn

    async def queue_frame(self, frame: object) -> None:
        self.queued.append(frame)

    async def cancel(self) -> None:
        return None


async def test_start_holds_the_channel_for_the_whole_run(monkeypatch) -> None:
    timeline: list[str] = []
    given: dict = {}

    @asynccontextmanager
    async def channel(call_context, handlers):
        given.update(context=call_context, handlers=handlers)
        timeline.append("subscribed")
        try:
            yield
        finally:
            timeline.append("unsubscribed")

    async def run(_runner, _worker):
        timeline.append("running")

    monkeypatch.setattr(cs, "control_channel", channel)
    monkeypatch.setattr(cs, "run_until_pipeline_done", run)
    monkeypatch.setattr(cs, "PipelineWorker", _Worker)
    monkeypatch.setattr(cs, "WorkerRunner", MagicMock)
    context = _context()
    session = cs.CallSession(
        call_context=context,  # type: ignore[arg-type]
        transport=_Worker(),
        pipeline=SimpleNamespace(processors=[]),  # type: ignore[arg-type]
    )
    for name in ("_update_call_status", "_trigger_first_response", "_finalize_call"):
        monkeypatch.setattr(
            session, name, AsyncMock(side_effect=lambda *_, n=name: timeline.append(n))
        )
    monkeypatch.setattr(session, "_build_telemetry", AsyncMock(return_value=([], {})))
    monkeypatch.setattr(session, "_arm_idle_guard", lambda: None)

    await session.start()

    # Open before the call reads as live; closed before it is finalized.
    assert timeline == [
        "subscribed",
        "_update_call_status",
        "_trigger_first_response",
        "running",
        "unsubscribed",
        "_finalize_call",
    ]
    assert given["context"] is context
    assert {MESSAGE, HANDOFF} <= set(given["handlers"])

    # The handler it was given reaches this call's own pipeline.
    await given["handlers"][MESSAGE]({"message": "hi", "role": "system"})
    (frame,) = session._task.queued  # type: ignore[union-attr]
    assert frame.messages == [{"role": "system", "content": "hi"}]


def test_every_voice_entry_point_runs_the_call_through_call_session_start() -> None:
    """A module that builds a voice transport must hand the call to
    `CallSession.start()` — through `start_call_pipeline`, or
    `build_call_pipeline` and then `.start()` — or its calls have no channel."""
    src = Path(turncall.__file__).parent
    builders = {}
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text())
        called = {
            getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        if called & VOICE_FACTORIES and path.name != "transport_factory.py":
            builders[path.relative_to(src).as_posix()] = called

    assert builders, "found no voice transport call sites"
    unsubscribed = [
        module
        for module, called in builders.items()
        if "start_call_pipeline" not in called
        and not {"build_call_pipeline", "start"} <= called
    ]
    assert unsubscribed == []
    # The three this was written against; a fourth must pass the check above.
    assert {"webhooks/media_stream.py", "api/v1/webrtc.py"} <= set(builders)
    assert any("whatsapp" in m for m in builders)


def test_start_call_pipeline_runs_the_session_it_built() -> None:
    """The fire-and-forget transports' half of the check above."""
    from turncall.orchestrator import pipeline_builder

    tree = ast.parse(Path(pipeline_builder.__file__).read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_call_pipeline"
    )
    starts = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "attr", None) == "start"
    ]
    assert starts, "start_call_pipeline no longer awaits CallSession.start()"


class TestTheChannel:
    async def test_an_eval_has_none(self, monkeypatch) -> None:
        serve = MagicMock()
        monkeypatch.setattr(live_control.live_call_channel, "serve", serve)
        async with live_control.control_channel(_context(is_eval=True), {}):
            pass
        serve.assert_not_called()

    async def test_no_redis_costs_the_channel_never_the_call(self, monkeypatch) -> None:
        from turncall.storage import redis

        monkeypatch.setattr(redis, "_client", None)
        monkeypatch.setattr(redis, "_url", None)
        ran = False
        async with live_control.control_channel(_context(), {}):
            ran = True
        assert ran


class TestTheMessageHandler:
    async def test_cascade_appends_to_the_context_without_a_turn(self) -> None:
        queued: list = []

        async def queue(frame):
            queued.append(frame)

        handler = live_control.message_handler(queue, pipeline_mode="cascade")
        applied = await handler({"message": "VIP", "role": "system"})

        assert applied == {"role": "system"}
        (frame,) = queued
        assert frame.messages == [{"role": "system", "content": "VIP"}]
        assert frame.run_llm is False

    async def test_s2s_refuses_rather_than_report_a_message_nothing_reads(
        self,
    ) -> None:
        queue = AsyncMock()
        handler = live_control.message_handler(queue, pipeline_mode="s2s")
        with pytest.raises(ControlRefused, match="speech-to-speech"):
            await handler({"message": "VIP", "role": "system"})
        queue.assert_not_awaited()


class TestTheHandoffHandler:
    async def test_openai_live_refuses_as_its_tool_does(self, monkeypatch) -> None:
        """The schema refuses `handoff_to_agent` on openai_live; the API must
        not do through the side door what the model cannot."""
        from turncall.orchestrator import tool_bridge

        prepare = AsyncMock()
        monkeypatch.setattr(tool_bridge, "_prepare_handoff", prepare)
        handler = live_control.handoff_handler(
            _context(), lambda: None, pipeline_mode="s2s", s2s_provider="openai_live"
        )
        with pytest.raises(ControlRefused, match="openai_live"):
            await handler({"agent_id": str(uuid.uuid4())})
        prepare.assert_not_awaited()

    def test_it_finds_what_a_tool_call_would_be_handed(self) -> None:
        """The LLM, its context and the worker: `FunctionCallParams`' three,
        found in a real pipeline rather than handed over by pipecat."""
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
        )

        from tests.harness.stubs import StubLLM

        context = LLMContext()
        pair = LLMContextAggregatorPair(context=context)
        llm = StubLLM([])
        pipeline = Pipeline([pair.user(), llm, pair.assistant()])
        worker = object()

        stages = live_control.stages_of(pipeline, worker)

        assert stages == (llm, context, worker)
        assert live_control.stages_of(pipeline, None) is None


def test_the_api_never_commits_a_handoff_itself() -> None:
    """`call_control.handoff_to_agent` writes the record; only the call may
    call it, between preparing the switch and applying it (#144, #216)."""
    src = Path(turncall.__file__).parent
    callers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "handoff_to_agent"
    )
    assert callers == ["orchestrator/tool_bridge.py"], callers
