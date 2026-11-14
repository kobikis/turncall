"""A handoff's target and a number's stored agent go through agent acceptance (#480).

A stored agent saved before a newer rule existed still sits in `agents`; the
schema let it in then. `handoff` and `stored_call` read it as it is and refuse
it with the rule's own words, so a handoff fails in its prepare step — before
anything is committed — and says why, instead of an opaque "could not be
loaded"; the call stays where it was.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.services.agent_acceptance import (
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    accept_agent,
)

pytestmark = pytest.mark.unit

XAI_BASE_URL = {"llm": {"provider": "xai", "base_url": "https://gw.example/v1"}}
XAI_REASON = "base_url is not supported for the xai provider"
STORED_ORIGINS = [AgentOrigin.HANDOFF, AgentOrigin.STORED_CALL]


# --- the origins -----------------------------------------------------------------


@pytest.mark.parametrize("origin", STORED_ORIGINS)
async def test_a_stored_config_breaking_a_provider_rule_is_refused(origin) -> None:
    outcome = await accept_agent(
        {"system_prompt": "hi", **XAI_BASE_URL},
        project_id=uuid4(),
        origin=origin,
        session=None,
    )

    assert isinstance(outcome, AgentRefusal), outcome
    assert outcome.reason == XAI_REASON
    assert outcome.field == "llm"


@pytest.mark.parametrize("origin", STORED_ORIGINS)
async def test_a_stored_config_with_a_retired_key_still_runs(origin) -> None:
    """A key a later release removed is nothing to refuse the call over."""
    outcome = await accept_agent(
        {"system_prompt": "hi", "retired_setting": True},
        project_id=uuid4(),
        origin=origin,
        session=None,
    )

    assert isinstance(outcome, AcceptedAgent), outcome
    assert [w.field for w in outcome.warnings] == ["retired_setting"]


# --- the handoff's prepare step ----------------------------------------------------


class _SessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *exc):
        return False


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        call_id=uuid4(),
        project_id=uuid4(),
        agent_id=uuid4(),
        session_factory=_SessionFactory(),
        mcp_manager=None,
        tool_mocks=None,
        is_eval=False,
        records=True,
        has_database=True,
    )


def _stored(blob: dict[str, Any] | None):
    agent = (
        None
        if blob is None
        else SimpleNamespace(id=uuid4(), name="Billing", config_blob=blob)
    )
    return patch(
        "turncall.storage.repositories.agent_repo.get_agent_by_id",
        new=AsyncMock(return_value=agent),
    )


async def test_prepare_refuses_a_target_breaking_a_rule_with_its_reason() -> None:
    from turncall.orchestrator import tool_bridge

    with _stored({"system_prompt": "B", **XAI_BASE_URL}):
        prepared = await tool_bridge._prepare_handoff(
            {"agent_id": str(uuid4())}, _context()
        )

    assert isinstance(prepared, tool_bridge.HandoffNotPrepared), prepared
    assert XAI_REASON in prepared.message
    assert "stays on the current agent" in prepared.message


async def test_prepare_says_a_target_outside_the_project_was_not_found() -> None:
    from turncall.orchestrator import tool_bridge

    with _stored(None):
        prepared = await tool_bridge._prepare_handoff(
            {"agent_id": str(uuid4())}, _context()
        )

    assert isinstance(prepared, tool_bridge.HandoffNotPrepared), prepared
    assert "not found in this project" in prepared.message


async def test_a_refused_target_is_never_committed_and_the_model_reads_why() -> None:
    import json

    from turncall.orchestrator import tool_bridge

    committed = AsyncMock()
    params = SimpleNamespace(
        llm=MagicMock(),
        context=MagicMock(),
        pipeline_worker=SimpleNamespace(queue_frame=AsyncMock()),
    )
    with (
        _stored({"system_prompt": "B", **XAI_BASE_URL}),
        patch.object(tool_bridge, "_execute_builtin", new=committed),
    ):
        outcome = await tool_bridge.run_handoff(
            {"agent_id": str(uuid4())}, _context(), params
        )

    committed.assert_not_awaited()
    params.pipeline_worker.queue_frame.assert_not_awaited()
    assert outcome.applied is None
    result = json.loads(outcome.result)
    assert result["success"] is False
    assert XAI_REASON in result["message"]


async def test_the_live_api_refusal_carries_the_reason() -> None:
    """`POST /v1/calls/{id}/handoff` answers 409 with the handler's refusal."""
    from turncall.orchestrator.live_control import handoff_handler
    from turncall.services.live_call_channel import ControlRefused

    handler = handoff_handler(
        _context(),
        lambda: SimpleNamespace(
            llm=MagicMock(),
            context=MagicMock(),
            pipeline_worker=SimpleNamespace(queue_frame=AsyncMock()),
        ),
        pipeline_mode="cascade",
        s2s_provider=None,
    )
    with (
        _stored({"system_prompt": "B", **XAI_BASE_URL}),
        pytest.raises(ControlRefused, match=XAI_REASON),
    ):
        await handler({"agent_id": str(uuid4())})
