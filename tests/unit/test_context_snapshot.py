"""Context snapshots: what the model was given, one per agent segment (#177, #180).

Drives the snapshotter the way the pipeline does — a live context that keeps
changing, a handoff that clears it, the end of the call — and checks what it
hands to the store.
"""

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from turncall.orchestrator import context_snapshot
from turncall.orchestrator.context_snapshot import ContextSnapshotter
from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID

pytestmark = pytest.mark.unit


class _Context:
    def __init__(self):
        self.messages: list = []

    def get_messages(self):
        return self.messages

    def set_messages(self, messages):
        self.messages = messages


def _snapshotter(*, agent_id=None, store=True, is_eval=False):
    written: list[dict] = []

    async def write(snapshot: dict) -> None:
        written.append(snapshot)

    ctx = SimpleNamespace(
        call_id=uuid4(), agent_id=agent_id or uuid4(), is_eval=is_eval
    )
    context = _Context()
    snap = ContextSnapshotter(
        ctx,
        context=context,
        system_instruction="You are Riley. Caller: Jane.",
        store_context=store,
        write=write,
    )
    return snap, context, written, ctx


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


async def test_call_end_writes_one_snapshot_of_what_the_model_saw():
    snap, context, written, ctx = _snapshotter()
    context.messages = [
        {"role": "user", "content": "What time is it?"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "clock"}}]},
        {"role": "tool", "content": {"time": "12:42"}},
    ]
    await snap.flush()
    [snapshot] = written
    assert snapshot["segment"] == 0
    assert snapshot["agent_id"] == ctx.agent_id
    assert snapshot["system_instruction"] == "You are Riley. Caller: Jane."
    assert snapshot["messages"][2] == {"role": "tool", "content": {"time": "12:42"}}


async def test_snapshot_is_a_copy_not_a_live_view():
    snap, context, written, _ = _snapshotter()
    context.messages = [{"role": "user", "content": "hi"}]
    snap.capture_for_handoff(
        agent_id=uuid4(), system_instruction="next", store_context=True
    )
    context.messages.append({"role": "user", "content": "after"})
    await _settle()
    assert written[0]["messages"] == [{"role": "user", "content": "hi"}]


async def test_handoff_gives_one_segment_per_agent_in_order():
    first = uuid4()
    second = uuid4()
    snap, context, written, _ = _snapshotter(agent_id=first)
    context.messages = [{"role": "user", "content": "billing please"}]
    snap.capture_for_handoff(
        agent_id=second, system_instruction="You are billing.", store_context=True
    )
    context.set_messages([])  # what the handoff does next
    context.messages = [{"role": "user", "content": "my invoice"}]
    await snap.flush()
    await _settle()
    # The handoff's write is spawned, so it may land after the final one; the
    # segment number is what orders them.
    written.sort(key=lambda w: w["segment"])
    assert [(w["segment"], w["agent_id"]) for w in written] == [
        (0, first),
        (1, second),
    ]
    assert written[1]["system_instruction"] == "You are billing."
    assert written[1]["messages"] == [{"role": "user", "content": "my invoice"}]


async def test_switched_off_agent_writes_nothing_for_its_segment():
    snap, context, written, _ = _snapshotter(store=False)
    context.messages = [{"role": "user", "content": "my date of birth is ..."}]
    snap.capture_for_handoff(
        agent_id=uuid4(), system_instruction="open", store_context=True
    )
    await snap.flush()
    await _settle()
    assert [w["segment"] for w in written] == [1]


async def test_inline_agent_is_recorded_as_no_agent():
    snap, _, written, _ = _snapshotter(agent_id=DYNAMIC_AGENT_ID)
    await snap.flush()
    assert written[0]["agent_id"] is None


async def test_unserialisable_content_is_stringified_not_fatal():
    snap, context, written, _ = _snapshotter()
    context.messages = [{"role": "user", "content": object()}]
    await snap.flush()
    assert isinstance(written[0]["messages"][0]["content"], str)


def test_capture_for_handoff_never_raises():
    # It runs after the handoff committed (#144): a raise there is the
    # half-applied switch with a deterministic trigger.
    snap, context, _, _ = _snapshotter()
    context.get_messages = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    snap.capture_for_handoff(
        agent_id=uuid4(), system_instruction="x", store_context=True
    )


async def test_registry_is_per_call_and_skips_evals():
    live = SimpleNamespace(call_id=uuid4(), agent_id=uuid4(), is_eval=False)
    evalctx = SimpleNamespace(call_id=uuid4(), agent_id=uuid4(), is_eval=True)
    config = SimpleNamespace(observability=SimpleNamespace(store_context=True))
    context_snapshot.register(live, _Context(), "p", config)
    context_snapshot.register(evalctx, _Context(), "p", config)
    assert context_snapshot.for_call(live.call_id) is not None
    assert context_snapshot.for_call(evalctx.call_id) is None
    context_snapshot.discard(live.call_id)
    assert context_snapshot.for_call(live.call_id) is None
    assert isinstance(live.call_id, UUID)


async def test_applying_a_handoff_snapshots_the_outgoing_agent_first():
    """#180: the real handoff path, not the snapshotter alone."""
    from turncall.domain.models import AgentConfig
    from turncall.orchestrator.tool_bridge import (
        PreparedHandoff,
        _apply_handoff_context,
    )

    first, second = uuid4(), uuid4()
    ctx = SimpleNamespace(call_id=uuid4(), agent_id=first, is_eval=False)
    context = _Context()
    context.messages = [{"role": "user", "content": "put me through to billing"}]
    config = SimpleNamespace(observability=SimpleNamespace(store_context=True))
    snap = context_snapshot.register(ctx, context, "You are Riley.", config)
    written: list[dict] = []

    async def write(snapshot):
        written.append(snapshot)

    snap._write = write
    queued: list = []

    async def queue_frame(frame):
        queued.append(frame)

    params = SimpleNamespace(
        llm=None,
        context=context,
        pipeline_worker=SimpleNamespace(queue_frame=queue_frame),
    )
    prepared = PreparedHandoff(
        name="Billing",
        config=AgentConfig(),
        frames=(),
        agent_id=second,
        system_instruction="You are billing.",
    )
    await _apply_handoff_context(prepared, ctx, params)
    context.messages = [{"role": "user", "content": "my invoice"}]
    await context_snapshot.flush_call(ctx.call_id)
    await _settle()

    written.sort(key=lambda w: w["segment"])
    assert [(w["segment"], w["agent_id"]) for w in written] == [(0, first), (1, second)]
    assert written[0]["messages"] == [
        {"role": "user", "content": "put me through to billing"}
    ]
    assert written[1]["system_instruction"] == "You are billing."
    assert context_snapshot.for_call(ctx.call_id) is None
