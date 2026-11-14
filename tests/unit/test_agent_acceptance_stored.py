"""Agent acceptance for an agent id (#474, #480, #484): one of the
project's, or refused; and the gpt-live-1 delegate its tools and linked
knowledge bases predict."""

import uuid
from typing import Any

import pytest

from tests.acceptance_support import (
    CALL_INIT,
    OTHER_PROJECT,
    PROJECT,
)
from tests.acceptance_support import accept as _accept
from tests.acceptance_support import codes as _codes
from tests.acceptance_support import refused as _refused
from tests.agent_table import agent_row, agent_table
from turncall.services.agent_acceptance import (
    POLICIES,
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    UnknownFields,
)

pytestmark = pytest.mark.unit

BROKEN_GRAPH = {
    "name": "billing",
    "steps": [
        {"id": "a", "kind": "ask", "start": True, "moves": [{"to": "missing"}]},
        {"id": "b", "kind": "end"},
    ],
}


# --- an agent id: one of the project's, or refused ------------------------------


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> dict[uuid.UUID, Any]:
    return agent_table(monkeypatch)


async def test_an_agent_id_in_the_project_is_accepted_with_its_config(agents) -> None:
    row = agent_row(PROJECT, {"system_prompt": "Stored"})
    agents[row.id] = row

    accepted = await _accept(row.id, session=object())

    assert isinstance(accepted, AcceptedAgent)
    assert accepted.agent_id == row.id
    assert accepted.config.system_prompt == "Stored"


async def test_another_projects_agent_id_is_refused(agents) -> None:
    row = agent_row(OTHER_PROJECT, {"system_prompt": "Theirs"})
    agents[row.id] = row

    refusal = await _accept(row.id, session=object())

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field == "agent_id"
    assert str(row.id) in refusal.reason
    assert "Theirs" not in refusal.reason


async def test_an_agent_id_that_names_no_agent_is_refused(agents) -> None:
    refusal = await _accept(uuid.uuid4(), session=object())

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field == "agent_id"


async def test_a_stored_agents_auto_knowledge_link_on_s2s_is_a_warning(agents) -> None:
    row = agent_row(
        PROJECT,
        {"pipeline_mode": "s2s", "s2s": {"provider": "openai"}},
        kb_modes=["auto"],
    )
    agents[row.id] = row

    accepted = await _accept(row.id, session=object())

    assert isinstance(accepted, AcceptedAgent)
    assert _codes(accepted) == ["knowledge_mode_unsupported"]


async def test_a_stored_agent_breaking_a_rule_is_refused_like_an_inline_one(
    agents,
) -> None:
    row = agent_row(PROJECT, {"tts": {"provider": "xai", "speed": 3.0}})
    agents[row.id] = row

    refusal = await _accept(row.id, session=object())

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field == "tts"


@pytest.mark.parametrize(
    "origin", [CALL_INIT, AgentOrigin.HANDOFF, AgentOrigin.STORED_CALL]
)
async def test_a_stored_blobs_unknown_keys_are_not_checked(
    agents, origin: AgentOrigin
) -> None:
    """The schema wrote it: a key a later release removed is nothing to warn
    about, by id on every origin."""
    row = agent_row(PROJECT, {"system_prompt": "Stored", "voice_style": "warm"})
    agents[row.id] = row

    accepted = await _accept(row.id, session=object(), origin=origin)

    assert isinstance(accepted, AcceptedAgent)
    assert accepted.warnings == ()


def test_an_origin_that_only_reads_stored_agents_says_it_checks_no_keys() -> None:
    stored_only = (
        AgentOrigin.HANDOFF,
        AgentOrigin.EVAL_TARGET,
        AgentOrigin.STORED_CALL,
    )
    for origin in stored_only:
        assert POLICIES[origin].unknown_fields is UnknownFields.UNCHECKED
    for origin in set(AgentOrigin) - set(stored_only):
        assert POLICIES[origin].unknown_fields is not UnknownFields.UNCHECKED


# --- gpt-live-1's client delegate: the predicate (#484) ---------------------------

XAI_GATEWAY_LLM = {"provider": "xai", "base_url": "https://gw.example/v1"}
XAI_GATEWAY_REASON = "base_url is not supported for the xai provider"
GPT_LIVE_1 = {"pipeline_mode": "s2s", "s2s": {"provider": "openai_live"}}


@pytest.mark.parametrize("origin", [AgentOrigin.STORED_CALL, AgentOrigin.HANDOFF])
async def test_a_linked_tool_knowledge_base_gives_gpt_live_1_its_delegate(
    agents, origin
) -> None:
    """The build advertises `query_knowledge` for a linked tool-mode KB and
    builds a client delegate from the agent's `llm`, so its rule applies."""
    row = agent_row(PROJECT, {**GPT_LIVE_1, "llm": XAI_GATEWAY_LLM}, kb_modes=["tool"])
    agents[row.id] = row

    refusal = await _accept(row.id, origin=origin, session=object())

    assert isinstance(refusal, AgentRefusal), refusal
    assert (refusal.field, refusal.reason) == ("llm", XAI_GATEWAY_REASON)


async def test_a_linked_tool_knowledge_base_with_a_sound_delegate_is_accepted(
    agents,
) -> None:
    row = agent_row(
        PROJECT,
        {**GPT_LIVE_1, "llm": {"provider": "xai", "model": "grok-4.20-non-reasoning"}},
        kb_modes=["tool"],
    )
    agents[row.id] = row

    accepted = await _accept(row.id, origin=AgentOrigin.STORED_CALL, session=object())

    assert isinstance(accepted, AcceptedAgent), accepted


@pytest.mark.parametrize("modes", [[], ["prompt"], ["auto"]])
async def test_a_linked_knowledge_base_searched_by_no_tool_builds_no_delegate(
    agents, modes
) -> None:
    row = agent_row(PROJECT, {**GPT_LIVE_1, "llm": XAI_GATEWAY_LLM}, kb_modes=modes)
    agents[row.id] = row

    accepted = await _accept(row.id, origin=AgentOrigin.STORED_CALL, session=object())

    assert isinstance(accepted, AcceptedAgent), accepted


async def test_mcp_servers_count_as_tools_before_discovery() -> None:
    """Pinned over-refusal: acceptance runs before MCP connects, so servers
    are "may have tools", as at the API's save — a call whose discovery would
    have come back empty, and built no delegate, is refused all the same."""
    refusal = await _refused(
        {
            **GPT_LIVE_1,
            "mcp_servers": [
                {"name": "crm", "transport": "http", "url": "https://mcp.example/mcp"}
            ],
            "llm": XAI_GATEWAY_LLM,
        }
    )

    assert (refusal.field, refusal.reason) == ("llm", XAI_GATEWAY_REASON)


async def test_an_inline_tool_knowledge_base_gives_gpt_live_1_its_delegate() -> None:
    refusal = await _refused(
        {
            **GPT_LIVE_1,
            "knowledge_bases": [
                {"knowledge_base_id": str(uuid.uuid4()), "mode": "tool"}
            ],
            "llm": XAI_GATEWAY_LLM,
        }
    )

    assert (refusal.field, refusal.reason) == ("llm", XAI_GATEWAY_REASON)


class _NoDatabase:
    """A session no read may touch: acceptance resolves no knowledge base."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"acceptance read the database ({name})")


async def test_an_inline_tool_knowledge_base_counts_before_it_resolves() -> None:
    """Pinned over-refusal, beside MCP's: the build resolves an inline KB
    against the call's project and skips one that is not there (#260), so a
    call whose only tool-mode KB will not resolve builds no delegate. It
    counts here all the same, read from the config alone: acceptance runs
    before anything resolves, and the `llm` is broken either way."""
    refusal = await _accept(
        {
            **GPT_LIVE_1,
            "knowledge_bases": [
                {"knowledge_base_id": str(uuid.uuid4()), "mode": "tool"}
            ],
            "llm": XAI_GATEWAY_LLM,
        },
        session=_NoDatabase(),
    )

    assert isinstance(refusal, AgentRefusal), refusal
    assert (refusal.field, refusal.reason) == ("llm", XAI_GATEWAY_REASON)
