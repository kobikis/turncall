"""An outbound call from a webhook-routed number runs call-init (#277).

Call-init ran on inbound calls only, so a config that can only be built per
call — an agent whose MCP headers carry a token that lives an hour — never
reached an outbound one. Now the dial asks the number's server too, before the
carrier is asked: call-init's agent wins, the dial's variables win a clash,
and a call-init that fails falls back to the dial's agent.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.events.server_events import ServerEventResponse
from turncall.services.outbound_dial import place_outbound_call

pytestmark = pytest.mark.unit

CALLS = "turncall.storage.repositories.call_repo"
MODULE = "turncall.services.outbound_dial"
PROJECT = uuid.uuid4()
INLINE = {"system_prompt": "Built for this call", "mcp_servers": []}


def _answer(data, success: bool = True) -> ServerEventResponse:
    return ServerEventResponse(success=success, status_code=200, data=data)


@pytest.fixture
def dial(monkeypatch):
    """Everything succeeds; the number is webhook-routed, call-init answers
    `state.answer`, and agents are looked up from `state.agents`."""
    state = SimpleNamespace()
    dial_agent = SimpleNamespace(id=uuid.uuid4(), project_id=PROJECT)
    state.dial_agent = dial_agent
    state.agents = {dial_agent.id: dial_agent}
    state.number = SimpleNamespace(
        id=uuid.uuid4(),
        e164_number="+15550000001",
        provider="twilio",
        routing_target_type="webhook",
        server_url="https://crm.example/call-init",
        server_url_secret="s3cret",
    )
    state.answer = _answer({"agent": INLINE})
    adapter = SimpleNamespace(initiate_outbound_call=AsyncMock(return_value="CA1"))
    settings = SimpleNamespace(server=SimpleNamespace(public_base_url="https://tc"))
    monkeypatch.setattr(f"{MODULE}.get_settings", lambda: settings)

    async def agent_by_id(_s, agent_id, **_kw):
        return state.agents.get(agent_id)

    with (
        patch(
            f"{MODULE}.suppression_repo.is_suppressed", AsyncMock(return_value=False)
        ),
        patch(
            f"{MODULE}.agent_repo.get_agent_by_id", AsyncMock(side_effect=agent_by_id)
        ),
        patch(
            "turncall.services.call_init_resolver.agent_repo.get_agent_by_id",
            AsyncMock(side_effect=agent_by_id),
        ),
        patch(
            f"{MODULE}.phone_number_repo.get_by_id",
            AsyncMock(side_effect=lambda *a, **k: state.number),
        ),
        patch(
            f"{MODULE}._carrier_account",
            AsyncMock(return_value=SimpleNamespace(adapter=lambda: adapter)),
        ),
        patch(f"{MODULE}.admit_outbound_call", AsyncMock()) as admit,
        patch(f"{MODULE}.release_live_call", AsyncMock()),
        patch(
            f"{MODULE}.send_call_init",
            AsyncMock(side_effect=lambda *a, **k: state.answer),
        ) as call_init,
        patch(
            f"{CALLS}.create_call",
            AsyncMock(side_effect=lambda s, **kw: SimpleNamespace(id=kw["call_id"])),
        ) as create,
        patch(
            f"{CALLS}.update_call_status",
            AsyncMock(side_effect=lambda s, cid, **kw: SimpleNamespace(id=cid)),
        ),
        patch(f"{CALLS}.get_next_sequence_number", AsyncMock(return_value=1)),
        patch(f"{CALLS}.create_call_event", AsyncMock()),
    ):
        state.call_init, state.create, state.admit = call_init, create, admit
        yield state


async def _place(state, metadata=None, call_id=None):
    await place_outbound_call(
        AsyncMock(),
        project_id=PROJECT,
        agent_id=state.dial_agent.id,
        from_number_id=state.number.id,
        to_number="+15550000002",
        metadata=metadata or {},
        call_id=call_id,
    )
    return state.create.await_args.kwargs


async def test_an_inline_agent_from_call_init_runs_the_call(dial):
    call_id = uuid.uuid4()
    created = await _place(dial, call_id=call_id)

    assert created["active_agent_id"] is None  # ADR-0017: no agent row
    assert created["metadata_json"]["dynamic_config"] == INLINE
    sent = dial.call_init.await_args
    assert sent.args == ("https://crm.example/call-init",)
    assert sent.kwargs["transport_type"] == "outboundPhoneCall"
    assert sent.kwargs["call_id"] == str(call_id)
    # Our number is the phone number, the callee the customer — as inbound.
    assert sent.kwargs["to_number"] == "+15550000001"
    assert sent.kwargs["from_number"] == "+15550000002"
    assert sent.kwargs["secret"] == "s3cret"
    assert sent.kwargs["agent_id"] == str(dial.dial_agent.id)


async def test_an_agent_id_from_call_init_wins_over_the_dials(dial):
    other = SimpleNamespace(id=uuid.uuid4(), project_id=PROJECT)
    dial.agents[other.id] = other
    dial.answer = _answer({"agent_id": str(other.id)})

    created = await _place(dial)

    assert created["active_agent_id"] == other.id
    assert "dynamic_config" not in created["metadata_json"]


async def test_call_init_with_no_agent_keeps_the_dials_and_adds_its_context(dial):
    dial.answer = _answer(
        {
            "variables": {"name": "Ada", "tier": "gold"},
            "metadata": {"crm_id": "C-1"},
            "dynamic_data": {"knowledge_context": "Open ticket #456"},
        }
    )

    created = await _place(
        dial,
        metadata={"campaign_id": "c1", "template_variables": {"tier": "platinum"}},
    )

    assert created["active_agent_id"] == dial.dial_agent.id
    assert created["metadata_json"] == {
        "campaign_id": "c1",
        # The dial's (a campaign target's) variables win a clash.
        "template_variables": {"name": "Ada", "tier": "platinum"},
        "metadata": {"crm_id": "C-1"},
        "knowledge_context": "Open ticket #456",
    }
    # And the server is shown them.
    assert dial.call_init.await_args.kwargs["variables"] == {"tier": "platinum"}


STRANGER = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
# Context written for an agent that will not run: none of it is kept.
CONTEXT = {"variables": {"name": "Ada"}, "dynamic_data": {"knowledge_context": "k"}}


@pytest.mark.parametrize(
    "answer",
    [
        _answer(None, success=False),
        _answer({"agent_id": "not-a-uuid", **CONTEXT}),
        _answer({"agent_id": str(uuid.uuid4()), **CONTEXT}),  # no such agent
        _answer({"agent_id": str(STRANGER.id), **CONTEXT}),  # another project's
        # Would ring the callee into a call that fails at its first frame.
        _answer({"agent": {"max_call_duration_seconds": "forever"}, **CONTEXT}),
    ],
)
async def test_an_unusable_call_init_falls_back_to_the_dials_agent(dial, answer):
    dial.agents[STRANGER.id] = STRANGER
    dial.answer = answer

    created = await _place(dial, metadata={"crm": "C-9"})

    assert created["active_agent_id"] == dial.dial_agent.id
    assert created["metadata_json"] == {"crm": "C-9"}


async def test_a_number_routed_to_an_agent_asks_nobody(dial):
    dial.number.routing_target_type = "agent"

    created = await _place(dial)

    dial.call_init.assert_not_awaited()
    assert created["active_agent_id"] == dial.dial_agent.id


async def test_a_dial_the_limits_refuse_asks_nobody(dial):
    """Admission comes first: a refused dial costs the server no request, and
    a campaign's one-second reservation is re-taken before call-init's wait."""
    dial.admit.side_effect = RuntimeError("rate limited")

    with pytest.raises(RuntimeError):
        await _place(dial)

    dial.call_init.assert_not_awaited()
