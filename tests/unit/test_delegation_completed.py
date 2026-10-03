"""`delegation.completed`: one stored call event per gpt-live-1 delegation (#247).

Driven at the built live service: a real `create_pipeline`, then the server
events gpt-live-1 would send. The backend job is pipecat's private
`_delegate_to_backend`, faked per outcome; the write is the call-event store.
"""

import asyncio
import inspect
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.pipeline.job_context import JobError
from pipecat.services.openai.live import events

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
_LLM_MODULE = "pipecat.services.openai.live.llm"


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
    return live


async def _delegate(live: Any, target: str = "client", id: str = "d1") -> None:
    tasks: list[asyncio.Task[Any]] = []

    def _create_task(coro: Any, name: str | None = None) -> asyncio.Task[Any]:
        tasks.append(asyncio.ensure_future(coro))
        return tasks[-1]

    with patch.object(live, "create_task", new=_create_task):
        await live._handle_server_event(
            events.SessionDelegationCreatedEvent(
                type="session.delegation.created",
                delegation=events.DelegationMetadata(id=id, target=target),
            )
        )
    await asyncio.gather(*tasks)
    # Event handlers run as tasks of their own.
    await asyncio.sleep(0)


def _answers(text: str) -> Any:
    async def _backend(*_: Any, on_update: Any, **__: Any) -> str:
        from pipecat.workers.llm.backend_llm_worker import BackendOutput

        await on_update(BackendOutput(text=text))
        return text

    return _backend


def _raises(error: Exception) -> Any:
    async def _backend(*_: Any, **__: Any) -> str:
        raise error

    return _backend


async def _silent(*_: Any, **__: Any) -> str:
    return ""


@pytest.fixture
def stored() -> Any:
    with patch.object(delegation_events, "store_call_event", new=AsyncMock()) as store:
        yield store


def _payloads(store: AsyncMock) -> list[dict[str, Any]]:
    assert all(
        c.args[1] == CallEventType.DELEGATION_COMPLETED for c in store.await_args_list
    )
    return [c.args[2] for c in store.await_args_list]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "outcome"),
    [
        (_answers("Booked for 8pm."), "answered"),
        (_raises(JobError("timeout")), "timed_out"),
        (_raises(JobError("worker 'delegate' errored")), "failed"),
        (_raises(RuntimeError("boom")), "failed"),
        (_silent, "failed"),
    ],
)
async def test_client_mode_records_each_outcome(
    stored: AsyncMock, backend: Any, outcome: str
) -> None:
    call_context = _call_context()
    live = _live({}, call_context, tools=[_BOOK])
    with patch(f"{_LLM_MODULE}._delegate_to_backend", new=backend):
        await _delegate(live)

    [payload] = _payloads(stored)
    assert payload["mode"] == "client"
    assert payload["outcome"] == outcome
    assert isinstance(payload["duration_ms"], int) and payload["duration_ms"] >= 0
    assert stored.await_args.args[0] is call_context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_event_per_delegation(stored: AsyncMock) -> None:
    live = _live({}, _call_context(), tools=[_BOOK])
    with patch(f"{_LLM_MODULE}._delegate_to_backend", new=_answers("ok")):
        await _delegate(live, id="d1")
        await _delegate(live, id="d2")
    assert [p["outcome"] for p in _payloads(stored)] == ["answered", "answered"]


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
    on the delegate, not gone."""
    live = _live({}, _call_context(), tools=[_BOOK])
    release = asyncio.Event()

    async def _working(*_: Any, **__: Any) -> str:
        await release.wait()
        return ""

    with (
        patch(f"{_LLM_MODULE}._delegate_to_backend", new=_working),
        patch.object(
            live, "create_task", new=lambda coro, name=None: asyncio.ensure_future(coro)
        ),
    ):
        await live._handle_server_event(
            events.SessionDelegationCreatedEvent(
                type="session.delegation.created",
                delegation=events.DelegationMetadata(id="d1", target="client"),
            )
        )
        assert live.delegating
        release.set()
        await live._delegation_tasks["d1"]
    await asyncio.sleep(0)
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
    with patch(f"{_LLM_MODULE}._delegate_to_backend", new=_answers("ok")):
        await _delegate(live)
    stored.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_never_dispatched_as_a_webhook() -> None:
    """Stored through the call-event repo, and nothing else."""
    call_context = _call_context()
    session = MagicMock(commit=AsyncMock())
    call_context.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=session
    )
    call_context.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    live = _live({}, call_context, tools=[_BOOK])
    with (
        patch(f"{_LLM_MODULE}._delegate_to_backend", new=_answers("ok")),
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
        await _delegate(live)
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
        "_run_client_delegation",
        "_handle_evt_delegation_created",
        "_handle_evt_response",
        "_send_context_append",
    ):
        assert hasattr(llm.OpenAILiveLLMService, name), name
    assert callable(llm._delegate_to_backend)
    assert "call_ids" in llm._PendingResponse.__dataclass_fields__
    source = inspect.getsource(llm.OpenAILiveLLMService._run_client_delegation)
    assert delegation_events.NO_OUTPUT in source
    assert delegation_events.NOT_COMPLETED in source  # #262
