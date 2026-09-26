"""The handoff switch must not suspend once it has started mutating state.

#136 made the two queued frames survive an interruption's *drain*. That is
half of what an interruption does. The other half cancels every in-flight
function call registered `cancel_on_interruption=True` — which
`handoff_to_agent` is, being a sync tool — and no flag saves a frame the
handler never got to build.

`call_control.handoff_to_agent` commits `status=HANDED_OFF` and
`active_agent_id=<target>` before the pipeline is touched at all. So a
suspension point between that commit and the last queued frame leaves the call
record saying the handoff succeeded while the live pipeline keeps the previous
agent's prompt and tools for the rest of the call — logged nowhere, because
`CancelledError` is a BaseException and slips past `except Exception`.

The fix is to have nothing to wait for: the target's config is loaded before
the switch is committed, so applying it is pure computation plus two
non-suspending puts. These tests pin that, not the loading. #144.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, ToolDefinition


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="d",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="http://x/hook",
    )


def _target(config: AgentConfig):
    """A handoff already prepared — frames built, nothing committed yet."""
    from pipecat.frames.frames import LLMSetToolsFrame, LLMUpdateSettingsFrame
    from pipecat.processors.aggregators.llm_context import NOT_GIVEN
    from pipecat.services.llm_service import LLMSettings

    from turncall.orchestrator.tool_bridge import (
        PreparedHandoff,
        _mark_uninterruptible,
    )

    return PreparedHandoff(
        name="Billing",
        config=config,
        frames=(
            _mark_uninterruptible(
                LLMUpdateSettingsFrame(
                    delta=LLMSettings(system_instruction="you are B")
                )
            ),
            _mark_uninterruptible(LLMSetToolsFrame(tools=NOT_GIVEN)),
        ),
    )


def _params(queue):
    async def queue_frame(frame):
        queue.append(frame)

    return SimpleNamespace(
        llm=MagicMock(),
        context=MagicMock(),
        pipeline_worker=SimpleNamespace(queue_frame=queue_frame),
    )


def _context():
    return SimpleNamespace(
        call_id=uuid4(),
        project_id=uuid4(),
        agent_id=uuid4(),
        session_factory=MagicMock(),
        mcp_manager=None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_switch_completes_within_one_scheduling_slice():
    """The invariant itself. A coroutine that never yields cannot be
    cancelled partway through, so this is what makes the switch atomic —
    and it fails the moment someone adds an `await` to it."""
    from turncall.orchestrator.tool_bridge import _apply_handoff_context

    queue: list = []
    with patch("turncall.orchestrator.tool_bridge.register_tools"):
        task = asyncio.create_task(
            _apply_handoff_context(
                _target(AgentConfig(tools=[_tool("refund")])),
                _context(),
                _params(queue),
            )
        )
        await asyncio.sleep(0)
        done = task.done()
        await task

    assert done, (
        "the handoff switch suspended partway through — an interruption can "
        "now be delivered inside it, leaving the call record handed off while "
        "the pipeline keeps the previous agent"
    )
    assert len(queue) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelling_the_switch_once_it_has_begun_applies_all_of_it_or_none():
    """Cancellation requested at the worst instant — the first mutation, with
    the context just cleared and neither frame queued yet. The switch either
    never ran or ran completely; never the middle.

    Cancelling *before* the coroutine's first slice would prove nothing: a
    task that never started is trivially all-or-nothing.
    """
    from turncall.orchestrator.tool_bridge import _apply_handoff_context

    queue: list = []
    params = _params(queue)
    holder: list[asyncio.Task] = []
    cleared: list[bool] = []

    def clear_and_interrupt(_messages):
        cleared.append(True)
        holder[0].cancel()  # the caller barges in, mid-switch

    params.context.set_messages = clear_and_interrupt

    with patch("turncall.orchestrator.tool_bridge.register_tools"):
        holder.append(
            asyncio.create_task(
                _apply_handoff_context(
                    _target(AgentConfig(tools=[_tool("refund")])), _context(), params
                )
            )
        )
        await asyncio.gather(*holder, return_exceptions=True)

    assert (len(queue), bool(cleared)) in {(0, False), (2, True)}, (
        f"half-applied: {len(queue)} frame(s) queued, context cleared={bool(cleared)}"
    )


@pytest.mark.asyncio
async def test_the_handoff_is_prepared_before_the_switch_is_committed():
    """Ordering is the whole fix: the DB round-trip and every fallible step
    have to happen before `call_control.handoff_to_agent` commits."""
    from turncall.orchestrator import tool_bridge

    order: list[str] = []

    async def load(*_a, **_k):
        order.append("load")
        return _target(AgentConfig(tools=[_tool("refund")]))

    async def execute(*_a, **_k):
        order.append("commit")
        return '{"success": true}'

    async def apply(*_a, **_k):
        order.append("apply")

    llm = MagicMock()
    registered: dict = {}
    llm.register_function.side_effect = lambda name, handler, **_: registered.update(
        {name: handler}
    )

    with (
        patch.object(tool_bridge, "_prepare_handoff", new=load),
        patch.object(tool_bridge, "_execute_builtin", new=execute),
        patch.object(tool_bridge, "_apply_handoff_context", new=apply),
        patch.object(tool_bridge, "_log_tool_result", new=AsyncMock()),
    ):
        tool_bridge.register_tools(
            llm,
            [
                ToolDefinition(
                    name="handoff_to_agent",
                    description="d",
                    parameters_schema={"type": "object", "properties": {}},
                )
            ],
            SimpleNamespace(
                call_id=uuid4(),
                project_id=uuid4(),
                agent_id=uuid4(),
                session_factory=MagicMock(),
                mcp_manager=None,
                tool_mocks=None,
                is_eval=False,
            ),
        )
        await registered["handoff_to_agent"](
            SimpleNamespace(
                function_name="handoff_to_agent",
                arguments={"agent_id": str(uuid4())},
                result_callback=AsyncMock(),
                llm=llm,
                context=MagicMock(),
                pipeline_worker=SimpleNamespace(queue_frame=AsyncMock()),
            )
        )

    assert order == ["load", "commit", "apply"], (
        "the target config must be read before the switch is committed — a DB "
        f"round-trip after it is the widest cancellation window there is. got {order}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_switch_the_database_refused_leaves_the_pipeline_alone():
    """#144 upside down. Preparing the handoff succeeds, but
    `call_control.handoff_to_agent` refuses the switch — the call is not
    in_progress, say. Applying it anyway would leave the pipeline speaking as
    the target agent while the call record never moved."""
    from turncall.orchestrator import tool_bridge

    applied: list[str] = []

    async def prepare(*_a, **_k):
        return _target(AgentConfig(tools=[_tool("refund")]))

    async def refuse(*_a, **_k):
        return '{"success": false, "message": "Can only handoff from in_progress"}'

    async def apply(*_a, **_k):
        applied.append("applied")

    with (
        patch.object(tool_bridge, "_prepare_handoff", new=prepare),
        patch.object(tool_bridge, "_execute_builtin", new=refuse),
        patch.object(tool_bridge, "_apply_handoff_context", new=apply),
        patch.object(tool_bridge, "_log_tool_result", new=AsyncMock()),
    ):
        await _run_handoff_tool()

    assert applied == [], (
        "the pipeline switched to an agent the database refused to hand off to"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_handoff_that_cannot_be_built_is_never_committed_to_the_pipeline():
    """The deterministic twin of the cancellation race: a target whose prompt
    or tool schema cannot be built. Failing *before* the commit means the
    switch is simply not prepared, so nothing is applied."""
    from turncall.orchestrator import pipeline_factory, tool_bridge

    agent = SimpleNamespace(
        name="Billing",
        config_blob=AgentConfig(tools=[_tool("refund")]).model_dump(mode="json"),
    )

    def explode(_config):
        raise ValueError("bad prompt template")

    class _SF:
        def __call__(self):
            return self

        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *e):
            return False

    ctx = _context()
    ctx.session_factory = _SF()

    with (
        patch(
            "turncall.storage.repositories.agent_repo.get_agent_by_id",
            new=AsyncMock(return_value=agent),
        ),
        patch.object(pipeline_factory, "_build_system_instruction", new=explode),
    ):
        prepared = await tool_bridge._prepare_handoff({"agent_id": str(uuid4())}, ctx)

    assert prepared is None, (
        "a handoff that cannot be built was still handed to the applier — it "
        "would raise after the switch was committed, stranding the pipeline "
        "on the previous agent"
    )


async def _run_handoff_tool() -> None:
    """Drive the registered `handoff_to_agent` handler once."""
    from turncall.domain.models import ToolDefinition as TD
    from turncall.orchestrator import tool_bridge

    llm = MagicMock()
    registered: dict = {}
    llm.register_function.side_effect = lambda name, handler, **_: registered.update(
        {name: handler}
    )
    tool_bridge.register_tools(
        llm,
        [
            TD(
                name="handoff_to_agent",
                description="d",
                parameters_schema={"type": "object", "properties": {}},
            )
        ],
        SimpleNamespace(
            call_id=uuid4(),
            project_id=uuid4(),
            agent_id=uuid4(),
            session_factory=MagicMock(),
            mcp_manager=None,
            tool_mocks=None,
            is_eval=False,
        ),
    )
    await registered["handoff_to_agent"](
        SimpleNamespace(
            function_name="handoff_to_agent",
            arguments={"agent_id": str(uuid4())},
            result_callback=AsyncMock(),
            llm=llm,
            context=MagicMock(),
            pipeline_worker=SimpleNamespace(queue_frame=AsyncMock()),
        )
    )
