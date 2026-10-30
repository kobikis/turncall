"""The per-turn context message (#441): one message for this turn only, put
right before the caller's turn and taken out on the next.

KB auto mode's passages ride on it, and so does the procedure window (#442),
so the two must never take each other's message out.
"""

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext

from turncall.orchestrator.turn_message import replace_turn_message

_KB = "[Knowledge Base Context]"
_WINDOW = "[Procedure]"


def _maker(marker: str, text: str, seen: list | None = None):
    async def make(messages, current):
        if seen is not None:
            seen.append((list(messages), current))
        return {"role": "developer", "content": f"{marker} {text}"}

    return make


def _marked(context: LLMContext, marker: str) -> list[str]:
    return [
        m["content"]
        for m in context.get_messages()
        if str(m.get("content", "")).startswith(marker)
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_this_turns_message_goes_right_before_the_callers_turn():
    seen: list = []
    context = LLMContext(
        messages=[
            {"role": "assistant", "content": "Hi."},
            {"role": "user", "content": "Sushi?"},
        ]
    )

    await replace_turn_message(context, _KB, _maker(_KB, "salmon", seen))

    assert context.get_messages() == [
        {"role": "assistant", "content": "Hi."},
        {"role": "developer", "content": f"{_KB} salmon"},
        {"role": "user", "content": "Sushi?"},
    ]
    assert seen[0][1] == "Sushi?"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_previous_turns_message_is_taken_out():
    context = LLMContext(messages=[{"role": "user", "content": "Sushi?"}])
    await replace_turn_message(context, _KB, _maker(_KB, "salmon"))
    context.add_message({"role": "assistant", "content": "Salmon rolls."})
    context.add_message({"role": "user", "content": "Soup?"})

    await replace_turn_message(context, _KB, _maker(_KB, "miso"))

    assert _marked(context, _KB) == [f"{_KB} miso"]
    assert context.get_messages()[-2:] == [
        {"role": "developer", "content": f"{_KB} miso"},
        {"role": "user", "content": "Soup?"},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_markers_do_not_take_out_each_others_message():
    """KB passages and the procedure window share one context and each
    replace only their own, in either order, turn after turn."""
    context = LLMContext(messages=[{"role": "user", "content": "Sushi?"}])
    await replace_turn_message(context, _WINDOW, _maker(_WINDOW, "step 1"))
    await replace_turn_message(context, _KB, _maker(_KB, "salmon"))
    context.add_message({"role": "assistant", "content": "Salmon rolls."})
    context.add_message({"role": "user", "content": "Soup?"})
    await replace_turn_message(context, _KB, _maker(_KB, "miso"))
    await replace_turn_message(context, _WINDOW, _maker(_WINDOW, "step 2"))

    assert _marked(context, _KB) == [f"{_KB} miso"]
    assert _marked(context, _WINDOW) == [f"{_WINDOW} step 2"]
    assert context.get_messages()[-1] == {"role": "user", "content": "Soup?"}
    assert {m["content"] for m in context.get_messages()[-3:-1]} == {
        f"{_KB} miso",
        f"{_WINDOW} step 2",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_maker_never_sees_its_own_previous_message():
    seen: list = []
    context = LLMContext(messages=[{"role": "user", "content": "Sushi?"}])
    await replace_turn_message(context, _KB, _maker(_KB, "salmon"))
    context.add_message({"role": "user", "content": "Soup?"})

    await replace_turn_message(context, _KB, _maker(_KB, "miso", seen))

    messages, _ = seen[0]
    assert not any(str(m.get("content")).startswith(_KB) for m in messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_none_made_leaves_no_stale_message():
    context = LLMContext(messages=[{"role": "user", "content": "Sushi?"}])
    await replace_turn_message(context, _KB, _maker(_KB, "salmon"))
    context.add_message({"role": "user", "content": "Close when?"})

    async def nothing(_messages, _current):
        return None

    await replace_turn_message(context, _KB, nothing)

    assert _marked(context, _KB) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_context_not_ending_on_the_caller_makes_nothing():
    """A run the caller did not start (a tool result, an idle check-in) gets
    no message, and last turn's is still taken out."""
    seen: list = []
    context = LLMContext(messages=[{"role": "user", "content": "Sushi?"}])
    await replace_turn_message(context, _KB, _maker(_KB, "salmon"))
    context.add_message({"role": "assistant", "content": "Let me check."})

    await replace_turn_message(context, _KB, _maker(_KB, "miso", seen))

    assert seen == []
    assert _marked(context, _KB) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_message_added_while_the_work_is_out_is_kept():
    """The aggregators share the context and run on their own tasks: writing
    back the list read before the work would drop what landed meanwhile."""
    context = LLMContext(messages=[{"role": "user", "content": "Sushi?"}])

    async def make(_messages, _current):
        context.add_message({"role": "assistant", "content": "Earlier reply."})
        return {"role": "developer", "content": f"{_KB} salmon"}

    await replace_turn_message(context, _KB, make)

    assert context.get_messages() == [
        {"role": "developer", "content": f"{_KB} salmon"},
        {"role": "user", "content": "Sushi?"},
        {"role": "assistant", "content": "Earlier reply."},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_turn_in_content_parts_is_read_too():
    seen: list = []
    context = LLMContext(
        messages=[{"role": "user", "content": [{"type": "text", "text": "Dim sum?"}]}]
    )

    await replace_turn_message(context, _KB, _maker(_KB, "x", seen))

    assert seen[0][1] == "Dim sum?"
