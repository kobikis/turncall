"""`end_call` and `do_not_call` arm the hangup and let the model say goodbye.

A REST hangup from inside the tool cut off whatever the agent was still saying,
and a model that says goodbye and calls the tool in separate replies was never
heard at all. The tool now arms `HangupAfterReply` and returns its result as
usual; the model's reply to it is the goodbye, and the gate ends the call once
that has played (`test_hangup_after_reply.py`).
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.domain.models import ToolDefinition
from turncall.orchestrator import tool_bridge
from turncall.orchestrator.hangup import HangupArmFrame

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

OK = json.dumps({"success": True, "message": "ok", "details": None})
REFUSED = json.dumps({"success": False, "message": "no", "details": None})


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="d",
        parameters_schema={"type": "object", "properties": {}},
    )


async def _call(name: str, builtin_result: str, *, is_eval: bool = False):
    """Run the registered handler; return what reached the LLM and the pipeline."""
    events: list[tuple[str, object]] = []
    captured = {}

    class _LLM:
        def register_function(self, fn_name, fn, **_options):
            captured["handler"] = fn

        async def push_frame(self, frame, *_direction):
            events.append(("frame", frame))

    async def result_callback(result, *, properties=None):
        events.append(("result", (result, properties)))

    call_context = SimpleNamespace(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        mcp_manager=None,
        session_factory=AsyncMock(),
        tool_mocks=None,
        is_eval=is_eval,
    )
    llm = _LLM()
    with (
        patch.object(
            tool_bridge, "_execute_builtin", new=AsyncMock(return_value=builtin_result)
        ),
        patch.object(tool_bridge, "_spawn"),
    ):
        tool_bridge.register_tools(llm, [_tool(name)], call_context)
        await captured["handler"](
            SimpleNamespace(
                function_name=name,
                arguments={},
                result_callback=result_callback,
                llm=llm,
            )
        )
    return events


@pytest.mark.parametrize("name", ["end_call", "do_not_call"])
async def test_arms_the_hangup_then_lets_the_model_reply(name):
    events = await _call(name, OK)

    assert [kind for kind, _ in events] == ["frame", "result"]
    assert isinstance(events[0][1], HangupArmFrame)
    # A plain result: the model replies to it, and that reply is the goodbye.
    assert events[1][1][1] is None


@pytest.mark.parametrize("name", ["end_call", "do_not_call"])
async def test_a_refused_end_leaves_the_call_running(name):
    events = await _call(name, REFUSED)

    assert [kind for kind, _ in events] == ["result"]
    assert events[0][1][1] is None


async def test_an_eval_is_never_ended_from_the_tool():
    # `live` policy: the built-in really ran, but there is no call to end and
    # the harness owns the conversation's length.
    events = await _call("end_call", OK, is_eval=True)

    assert [kind for kind, _ in events] == ["result"]


async def test_other_builtins_still_let_the_model_reply():
    events = await _call("send_dtmf", OK)

    assert [kind for kind, _ in events] == ["result"]
    assert events[0][1][1] is None


@pytest.mark.parametrize("via_gate", [False, True])
async def test_a_barge_in_cannot_split_the_record_from_the_end(via_gate):
    """Built-ins are cancelled on interruption, and pipecat cancels the task
    even after the result is in. Once the end is recorded the hangup has to be
    armed too: a cancel between the two left the row saying the agent hung up
    on a call that kept running. `via_gate`: an openai_live client delegate,
    which marks the call's gate as ending directly (#245) — the live service
    arms it on relaying the delegate's goodbye (#262)."""
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()
    pushed: list[object] = []
    captured = {}

    class _LLM:
        def register_function(self, fn_name, fn, **_options):
            captured["handler"] = fn

        async def push_frame(self, frame, *_direction):
            pushed.append(frame)

    class _Gate:
        def begin_ending(self):
            pushed.append("ending")

    async def result_callback(result, *, properties=None):
        entered.set()
        await release.wait()  # the interruption lands here

    llm = _LLM()
    call_context = SimpleNamespace(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        mcp_manager=None,
        session_factory=AsyncMock(),
        tool_mocks=None,
        is_eval=False,
    )
    with (
        patch.object(tool_bridge, "_execute_builtin", new=AsyncMock(return_value=OK)),
        patch.object(tool_bridge, "_spawn"),
    ):
        tool_bridge.register_tools(
            llm, [_tool("end_call")], call_context, _Gate() if via_gate else None
        )
        task = asyncio.create_task(
            captured["handler"](
                SimpleNamespace(
                    function_name="end_call",
                    arguments={},
                    result_callback=result_callback,
                    llm=llm,
                )
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)

    expected = ["ending"] if via_gate else [HangupArmFrame]
    assert [f if isinstance(f, str) else type(f) for f in pushed] == expected
