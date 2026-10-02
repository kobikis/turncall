"""`end_call` and `do_not_call` end the call gracefully, after the goodbye.

A REST hangup from inside the tool cut off whatever the agent was still saying.
The tool now hands back a result that asks for no further reply, then pushes an
`EndWorkerFrame` downstream: pipecat queues the pipeline's `EndFrame` behind the
goodbye already on its way to the TTS, and the TTS and the output transport
drain before they stop. The transport closing is what hangs the carrier up.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import EndWorkerFrame

from turncall.domain.models import ToolDefinition
from turncall.orchestrator import tool_bridge

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
async def test_ends_through_the_pipeline_after_the_result(name):
    events = await _call(name, OK)

    assert [kind for kind, _ in events] == ["result", "frame"]
    _result, properties = events[0][1]
    # Nothing more from the model: a reply started now would be cut off.
    assert properties is not None and properties.run_llm is False
    frame = events[1][1]
    assert isinstance(frame, EndWorkerFrame)


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
