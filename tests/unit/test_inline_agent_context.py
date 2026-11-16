"""A call running an inline agent has no agent row (ADR-0017).

Managed call-init can answer with an inline `agent` config instead of an
`agent_id`. Such a call has nothing real for `CallContext.agent_id` to carry,
so it carries `DYNAMIC_AGENT_ID`, and the raw config is stored on the call row
as `dynamic_config`. The call module holds both rules for every voice edge:
opening writes the row (`tests/integration/test_call_opening.py`,
`test_outbound_opening.py`), running builds the context
(`tests/integration/test_run_call.py`), and an inline agent runs end to end
on every transport in `test_voice_call_init_acceptance.py` — or is refused at
its media stream, the one context an edge still builds, in
`test_build_takes_accepted_agent.py`.
"""

from uuid import UUID

import pytest

from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID


@pytest.mark.unit
def test_the_sentinel_is_a_real_uuid() -> None:
    """It is passed where a UUID is declared, and looked up in the database —
    it has to be one, and it must match no agent that exists."""
    assert isinstance(DYNAMIC_AGENT_ID, UUID)
    assert DYNAMIC_AGENT_ID == UUID(int=0)


# --- procedures on an inline agent (#444) -------------------------------------
#
# Agent acceptance parses the inline config as an `AgentConfig` (#476, #481).
# A raise there would refuse the call without saying why; a silent drop would
# run it without its procedure. So the domain model accepts any procedures,
# broken ones included, and acceptance's graph rule is what refuses them,
# naming the problem.

_BROKEN_GRAPH = {
    "name": "broken",
    "steps": [{"id": "a", "kind": "ask", "moves": [{"to": "x"}]}],
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "procedures",
    [[_BROKEN_GRAPH], [{"name": "unreadable", "steps": "a, then b"}], "not a list"],
    ids=["broken graph", "unreadable entry", "not a list"],
)
def test_an_inline_config_with_bad_procedures_still_parses_for_the_build(
    procedures: object,
) -> None:
    from turncall.domain.models import AgentConfig
    from turncall.domain.procedure_graph import procedures_refusal

    config = AgentConfig.model_validate(
        {"system_prompt": "hi", "procedures": procedures}
    )

    assert config.procedures, "dropped: the call would run without it"
    assert procedures_refusal(config.procedures) is not None
