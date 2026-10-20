"""`delegation.completed`: one stored call event per gpt-live-1 delegation (#247).

Driven at the built live service: a real `create_pipeline`, then the server
events gpt-live-1 would send. In client mode the delegate's side is what
pipecat 1.13's backend session reports — outputs, idle, an error — fed to the
service as the session would; the write is the call-event store.
"""

import asyncio
import inspect
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.services.openai.live import events
from pipecat.workers.llm.backend_llm_worker import (
    BackendError,
    BackendIdle,
    BackendOutput,
)

from tests.conftest import creds
from turncall.domain.enums import CallEventType
from turncall.domain.models import AgentConfig
from turncall.orchestrator import delegation_events
from turncall.orchestrator.pipeline_factory import CallContext, create_pipeline

_EMPTY = {"type": "object", "properties": {}}
_BOOK = {
    "name": "book_table",
    "description": "Book a table at the restaurant.",
    "parameters_schema": _EMPTY,
    "webhook_url": "https://example.com/book",
}


def _call_context(**overrides: Any) -> CallContext:
    return CallContext(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="CA1",
        stream_sid="MZ1",
        session_factory=MagicMock(),
        **overrides,
    )


def _live(s2s: dict[str, Any], call_context: CallContext, **config: Any) -> Any:
    from pipecat.services.openai.live.llm import OpenAILiveLLMService

    agent = AgentConfig.model_validate(
        {"pipeline_mode": "s2s", "s2s": {"provider": "openai_live", **s2s}, **config}
    )
    with patch("turncall.orchestrator.call_recorder.attach_recorder"):
        pipeline = create_pipeline(
            config=agent,
            transport=MagicMock(),
            call_context=call_context,
            pipecat_settings=SimpleNamespace(),
            credentials=creds("sk-test"),
        )
    live = next(p for p in pipeline.processors if isinstance(p, OpenAILiveLLMService))
    # Nothing is connected: the session's sends and pushes go nowhere.
    live.send_client_event = AsyncMock()
    live.push_frame = AsyncMock()
    live._setup = MagicMock()
    # The delegate's session, attached: what each delegation is sent over.
    live._backend_session = SimpleNamespace(send=AsyncMock(return_value="idle"))
    live._backend_attached.set()
    live._attach_backend = AsyncMock()
    live._detach_backend = AsyncMock()
    # No task manager outside a running pipeline: plain asyncio tasks.
    live.tasks = []

    def _create_task(coro: Any, name: str | None = None, *_: Any) -> Any:
        live.tasks.append(asyncio.ensure_future(coro))
        return live.tasks[-1]

    async def _cancel_task(task: Any, *_: Any) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    live.create_task = _create_task
    live.cancel_task = _cancel_task
    return live


async def _created(live: Any, target: str = "client", id: str = "d1") -> None:
    await live._handle_server_event(
        events.SessionDelegationCreatedEvent(
            type="session.delegation.created",
            delegation=events.DelegationMetadata(id=id, target=target),
        )
    )


async def _settle(live: Any) -> None:
    # A timeout task fires, or is cancelled by the delegation ending first.
    await asyncio.gather(*live.tasks, return_exceptions=True)
    # Event handlers run as tasks of their own.
    await asyncio.sleep(0)


async def _delegate(
    live: Any, *reported: Any, target: str = "client", id: str = "d1"
) -> None:
    """gpt-live-1 hands work over; the delegate's session reports `reported`."""
    await _created(live, target, id)
    for event in reported:
        await live._on_backend_event(event)
    await _settle(live)


_ANSWERS = (BackendOutput(text="Booked for 8pm."), BackendIdle())


@pytest.fixture
def stored() -> Any:
    with patch.object(delegation_events, "store_call_event", new=AsyncMock()) as store:
        yield store


def _payloads(store: AsyncMock) -> list[dict[str, Any]]:
    assert all(
        c.args[1] == CallEventType.DELEGATION_COMPLETED for c in store.await_args_list
    )
    return [c.args[2] for c in store.await_args_list]


async def _times_out(live: Any) -> None:
    live._delegation.timeout_secs = 0
    await _delegate(live)


async def _send_fails(live: Any) -> None:
    live._backend_session.send.side_effect = RuntimeError("boom")
    await _delegate(live)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drive", "outcome"),
    [
        (lambda live: _delegate(live, *_ANSWERS), "answered"),
        (_times_out, "timed_out"),
        (lambda live: _delegate(live, BackendError(error="boom")), "failed"),
        (_send_fails, "failed"),
        # Nothing for the caller: idle with no output, or with notes only.
        (lambda live: _delegate(live, BackendIdle()), "failed"),
        (
            lambda live: _delegate(
                live,
                BackendOutput(text="Table 4 is free.", prefers_spoken=False),
                BackendIdle(),
            ),
            "failed",
        ),
        # An error after it answered still reads as one.
        (
            lambda live: _delegate(
                live, BackendOutput(text="Booking now."), BackendError(error="boom")
            ),
            "failed",
        ),
    ],
)
async def test_client_mode_records_each_outcome(
    stored: AsyncMock, drive: Any, outcome: str
) -> None:
    call_context = _call_context()
    live = _live({}, call_context, tools=[_BOOK])
    await drive(live)

    [payload] = _payloads(stored)
    assert payload["mode"] == "client"
    assert payload["outcome"] == outcome
    assert isinstance(payload["duration_ms"], int) and payload["duration_ms"] >= 0
    assert stored.await_args.args[0] is call_context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_event_per_delegation(stored: AsyncMock) -> None:
    live = _live({}, _call_context(), tools=[_BOOK])
    await _delegate(live, *_ANSWERS, id="d1")
    await _delegate(live, *_ANSWERS, id="d2")
    assert [p["outcome"] for p in _payloads(stored)] == ["answered", "answered"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_waiting_delegation_counts_its_wait(stored: AsyncMock) -> None:
    """pipecat runs one at a time; the second waits for the delegate to go
    idle, and the caller waited all along: its duration runs from creation."""
    live = _live({}, _call_context(), tools=[_BOOK])
    clock = iter([0.0, 1.0, 5.0, 8.0])  # d1 and d2 created, d1 ends, d2 ends
    with patch.object(
        delegation_events, "time", SimpleNamespace(monotonic=lambda: next(clock))
    ):
        await _created(live, id="d1")
        await _created(live, id="d2")
        assert live._backend_session.send.await_count == 1
        await live._on_backend_event(BackendOutput(text="Booked."))
        await live._on_backend_event(BackendIdle())
        assert live._backend_session.send.await_count == 2
        await live._on_backend_event(BackendOutput(text="And the cake."))
        await live._on_backend_event(BackendIdle())
        await _settle(live)

    assert [p["duration_ms"] for p in _payloads(stored)] == [5000, 7000]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_delegation_the_call_drops_has_no_outcome(stored: AsyncMock) -> None:
    """The session ending takes the running and the waiting with it: neither
    ended, the call did."""
    live = _live({}, _call_context(), tools=[_BOOK])
    await _created(live, id="d1")
    await _created(live, id="d2")
    await live._abandon_delegations()
    await _settle(live)

    stored.assert_not_awaited()
    assert not live.delegating


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_delegation_without_a_delegate_is_declined(stored: AsyncMock) -> None:
    """No tools, no delegation: pipecat declines whatever gpt-live-1 hands off."""
    live = _live({}, _call_context())
    await _delegate(live)
    [payload] = _payloads(stored)
    assert payload["outcome"] == "declined"
    assert payload["mode"] == "client"


def _response(
    inner: str, delegation_id: str | None = "d1", **event: Any
) -> events.ResponseEventEnvelope:
    return events.ResponseEventEnvelope(
        type="response.event",
        delegation_id=delegation_id,
        event={"type": inner, **event},
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "outcome"),
    [
        ("response.completed", "answered"),
        ("response.failed", "failed"),
        ("response.incomplete", "failed"),
    ],
)
async def test_responses_mode_records_the_final_response(
    stored: AsyncMock, terminal: str, outcome: str
) -> None:
    live = _live(
        {"delegation": {"mode": "responses", "model": "m"}},
        _call_context(),
        tools=[_BOOK],
    )
    await _delegate(live, target="responses")
    await live._handle_server_event(_response("response.created"))
    await live._handle_server_event(_response(terminal, response={}))
    await asyncio.sleep(0)

    [payload] = _payloads(stored)
    assert payload == {
        "mode": "responses",
        "duration_ms": payload["duration_ms"],
        "outcome": outcome,
    }


def _responses_live() -> Any:
    live = _live(
        {"delegation": {"mode": "responses", "model": "m"}},
        _call_context(),
        tools=[_BOOK],
    )
    live.run_function_calls = AsyncMock()
    return live


def _call(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "function_call",
        "status": "completed",
        "call_id": "c1",
        "name": "book_table",
        "arguments": "{}",
        **overrides,
    }


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["response.completed", "response.failed"])
async def test_responses_mode_waits_out_a_function_call_round(
    stored: AsyncMock, first: str
) -> None:
    """A response that made a function call is continued once the call is
    answered — pipecat continues a failed one too — and the delegation ends
    with the response after it."""
    live = _responses_live()
    await _delegate(live, target="responses")
    await live._handle_server_event(_response("response.created"))
    await live._handle_server_event(
        _response("response.output_item.done", item=_call())
    )
    await live._handle_server_event(_response(first, response={}))
    await live._send_function_call_output("c1", "{}")
    await asyncio.sleep(0)
    assert not stored.await_args_list

    await live._handle_server_event(_response("response.created"))
    await live._handle_server_event(_response("response.completed", response={}))
    await asyncio.sleep(0)
    assert [p["outcome"] for p in _payloads(stored)] == ["answered"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item", [_call(status="incomplete"), _call(call_id=None), _call(arguments="{")]
)
async def test_a_call_pipecat_skips_does_not_hold_the_delegation_open(
    stored: AsyncMock, item: dict[str, Any]
) -> None:
    """pipecat runs no continuation for a function call it refused, so the
    response that carried it is the last."""
    live = _responses_live()
    await _delegate(live, target="responses")
    await live._handle_server_event(_response("response.created"))
    await live._handle_server_event(_response("response.output_item.done", item=item))
    await live._handle_server_event(_response("response.completed", response={}))
    await asyncio.sleep(0)
    assert [p["outcome"] for p in _payloads(stored)] == ["answered"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delegating_while_a_client_delegate_works(stored: AsyncMock) -> None:
    """The idle guard holds off while it is true (#257): the caller is waiting
    on the delegate, not gone — the delegation running and the one waiting."""
    live = _live({}, _call_context(), tools=[_BOOK])
    await _created(live, id="d1")
    await _created(live, id="d2")
    assert live.delegating
    await live._on_backend_event(BackendIdle())
    assert live.delegating
    await live._on_backend_event(BackendIdle())
    await _settle(live)
    assert not live.delegating


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delegating_until_the_final_response(stored: AsyncMock) -> None:
    live = _responses_live()
    await _delegate(live, target="responses")
    assert live.delegating
    await live._handle_server_event(_response("response.created"))
    await live._handle_server_event(_response("response.completed", response={}))
    assert not live.delegating


@pytest.mark.unit
@pytest.mark.asyncio
async def test_none_in_an_eval(stored: AsyncMock) -> None:
    live = _live({}, _call_context(eval_run_id=uuid.uuid4()), tools=[_BOOK])
    await _delegate(live, *_ANSWERS)
    stored.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_never_dispatched_as_a_webhook() -> None:
    """Stored through the call-event repo, and nothing else."""
    call_context = _call_context()
    # `get`: the exit point reads the project's redaction (#313); none here.
    session = MagicMock(commit=AsyncMock(), get=AsyncMock(return_value=None))
    call_context.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=session
    )
    call_context.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    live = _live({}, call_context, tools=[_BOOK])
    with (
        patch(
            "turncall.storage.repositories.call_repo.get_next_sequence_number",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "turncall.storage.repositories.call_repo.create_call_event",
            new=AsyncMock(),
        ) as create,
        patch("turncall.events.dispatcher.dispatch_event", new=AsyncMock()) as dispatch,
    ):
        await _delegate(live, *_ANSWERS)
        await asyncio.sleep(0)
    assert create.await_args.kwargs["event_type"] == CallEventType.DELEGATION_COMPLETED
    assert create.await_args.kwargs["payload"]["outcome"] == "answered"
    dispatch.assert_not_awaited()


@pytest.mark.unit
def test_pipecats_private_delegation_surface_is_still_there() -> None:
    """The outcome is read from pipecat internals; a bump that moves them
    should fail here, naming them, rather than silently record nothing."""
    from pipecat.services.openai.live import llm

    for name in (
        "_finish_delegation",
        "_start_delegation",
        "_abandon_delegations",
        "_on_backend_event",
        "_handle_evt_delegation_created",
        "_handle_evt_response",
        "_send_context_append",
    ):
        assert hasattr(llm.OpenAILiveLLMService, name), name
    assert {"id", "answered", "timeout_task"} <= set(
        llm._LiveDelegation.__dataclass_fields__
    )
    assert "call_ids" in llm._PendingResponse.__dataclass_fields__
    source = inspect.getsource(llm.OpenAILiveLLMService)
    assert delegation_events.NO_OUTPUT in source
    assert delegation_events.NOT_COMPLETED in source  # #262
    # How a timeout is told apart from every other end.
    timeout = inspect.getsource(llm.OpenAILiveLLMService._delegation_timeout)
    assert timeout.index("current.timeout_task = None") < timeout.index(
        "self._finish_delegation()"
    )
    # A failed start is reported from within `_start_delegation` itself.
    start = inspect.getsource(llm.OpenAILiveLLMService._start_delegation)
    assert start.index("await self.push_error(") < start.index(
        "await self._finish_delegation()"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_start_failing_after_its_timeout_leaves_the_next_one_alone(
    stored: AsyncMock,
) -> None:
    """d1 waits for the delegate to attach; its timeout fires first, records it
    and starts d2. d1's start then fails: pipecat would finish d2 with it,
    though the delegate is working on d2."""
    live = _live({}, _call_context(), tools=[_BOOK])
    live._backend_attached.clear()
    live._attach_backend = AsyncMock(side_effect=lambda: live._backend_attached.set())
    live._backend_session.send.side_effect = [
        "idle",  # d2's, sent first
        RuntimeError("detached"),  # d1's, late
    ]
    live._delegation.timeout_secs = 0.01

    first = asyncio.ensure_future(_created(live, id="d1"))
    await asyncio.sleep(0)
    await _created(live, id="d2")  # waits behind d1
    await first

    assert [p["outcome"] for p in _payloads(stored)] == ["timed_out"]
    assert live._current_delegation.id == "d2"
    await live._on_backend_event(BackendOutput(text="Done."))
    await live._on_backend_event(BackendIdle())
    await _settle(live)
    assert [p["outcome"] for p in _payloads(stored)] == ["timed_out", "answered"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_wait_for_quiet_is_not_the_delegations(stored: AsyncMock) -> None:
    """A delegation that ends the call waits for gpt-live-1 to be quiet before
    its goodbye; that wait is not part of how long the delegation took."""
    from unittest.mock import PropertyMock

    from turncall.orchestrator.hangup import HangupAfterReply

    live = _live({}, _call_context(), tools=[_BOOK])
    clock = iter([0.0, 2.0, 9.0])  # created, ended, after the wait for quiet
    state = {"ending": False}
    with (
        patch.object(
            delegation_events, "time", SimpleNamespace(monotonic=lambda: next(clock))
        ),
        patch.object(
            HangupAfterReply,
            "ending",
            new=PropertyMock(side_effect=lambda: state["ending"]),
        ),
        patch.object(HangupAfterReply, "arm_when_quiet", new=AsyncMock()),
    ):
        await _created(live)
        state["ending"] = True
        await live._on_backend_event(BackendOutput(text="Goodbye!"))
        await live._on_backend_event(BackendIdle())
        await _settle(live)

    assert [p["duration_ms"] for p in _payloads(stored)] == [2000]
