"""Agent acceptance (#474, #476): whether an agent config can run.

Every case sends a config in through `accept_agent` and reads what comes out
— an accepted agent with these warnings, or a refusal naming this field. Only
the `call_init` origin is wired so far; the others raise until their slice
(#477-#481) wires them.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from turncall.domain.models import AgentConfig
from turncall.domain.procedure_graph import procedures_refusal
from turncall.orchestrator.deepgram_flux import MIXED_FLUX_GROUP
from turncall.services import agent_acceptance
from turncall.services.agent_acceptance import (
    POLICIES,
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    UnknownFields,
    accept_agent,
)

pytestmark = pytest.mark.unit

PROJECT = uuid.uuid4()
OTHER_PROJECT = uuid.uuid4()
CALL_INIT = AgentOrigin.CALL_INIT

BROKEN_GRAPH = {
    "name": "billing",
    "steps": [
        {"id": "a", "kind": "ask", "start": True, "moves": [{"to": "missing"}]},
        {"id": "b", "kind": "end"},
    ],
}


async def _accept(source: Any, **kwargs: Any) -> AcceptedAgent | AgentRefusal:
    return await accept_agent(
        source,
        project_id=kwargs.pop("project_id", PROJECT),
        origin=kwargs.pop("origin", CALL_INIT),
        session=kwargs.pop("session", None),
    )


async def _refused(config: dict[str, Any]) -> AgentRefusal:
    outcome = await _accept(config)
    assert isinstance(outcome, AgentRefusal), outcome
    return outcome


async def _accepted(config: dict[str, Any]) -> AcceptedAgent:
    outcome = await _accept(config)
    assert isinstance(outcome, AcceptedAgent), outcome
    return outcome


def _codes(accepted: AcceptedAgent) -> list[str]:
    return [warning.code for warning in accepted.warnings]


# --- the table ------------------------------------------------------------------


def test_every_origin_has_one_row_in_the_policy_table() -> None:
    assert set(POLICIES) == set(AgentOrigin)


def test_the_decided_rows_read_as_the_spec_decided_them() -> None:
    assert POLICIES[CALL_INIT].unknown_fields is UnknownFields.WARN
    for strict in (AgentOrigin.API, AgentOrigin.EVAL_INLINE, AgentOrigin.DEMO):
        assert POLICIES[strict].unknown_fields is UnknownFields.REFUSE
    assert POLICIES[AgentOrigin.API].plain_http is False
    for inline in (CALL_INIT, AgentOrigin.EVAL_INLINE, AgentOrigin.DEMO):
        assert POLICIES[inline].plain_http is True


@pytest.mark.parametrize(
    "origin", [origin for origin in AgentOrigin if POLICIES[origin].wired_by]
)
async def test_an_origin_not_wired_yet_raises_rather_than_guessing(
    origin: AgentOrigin,
) -> None:
    with pytest.raises(NotImplementedError, match=origin.value):
        await _accept({"system_prompt": "hi"}, origin=origin)


# --- what an inline call-init agent is ------------------------------------------


async def test_an_inline_config_is_accepted_with_no_agent_id() -> None:
    accepted = await _accepted({"system_prompt": "Hello"})

    assert accepted.agent_id is None
    assert accepted.config == AgentConfig(system_prompt="Hello")
    assert accepted.warnings == ()


async def test_inline_knowledge_bases_survive_acceptance() -> None:
    kb = str(uuid.uuid4())
    accepted = await _accepted(
        {"knowledge_bases": [{"knowledge_base_id": kb, "mode": "tool"}]}
    )

    assert [str(a.knowledge_base_id) for a in accepted.config.knowledge_bases] == [kb]


async def test_a_config_that_is_not_an_object_is_refused() -> None:
    refusal = await _refused(["system_prompt"])  # type: ignore[arg-type]

    assert refusal.field is None
    assert "object" in refusal.reason


async def test_a_config_the_domain_model_cannot_read_is_refused_naming_the_field() -> (
    None
):
    refusal = await _refused({"max_call_duration_seconds": "forever"})

    assert refusal.field == "max_call_duration_seconds"
    assert refusal.reason.startswith("max_call_duration_seconds: ")


async def test_a_nested_field_the_domain_model_cannot_read_is_named_by_its_path() -> (
    None
):
    refusal = await _refused({"llm": {"failover": [{"temperature": "hot"}]}})

    assert refusal.field == "llm.failover[0].temperature"


# --- unknown fields: warned about and dropped, never refused --------------------


async def test_an_unknown_top_level_field_is_warned_about_and_dropped() -> None:
    accepted = await _accepted({"system_prompt": "hi", "voice_style": "warm"})

    assert _codes(accepted) == ["unknown_field"]
    assert "voice_style" in accepted.warnings[0].message
    assert accepted.warnings[0].field == "voice_style"
    assert not hasattr(accepted.config, "voice_style")


async def test_each_unknown_field_is_its_own_warning() -> None:
    accepted = await _accepted({"zeta": 1, "alpha": 2})

    assert [w.field for w in accepted.warnings] == ["alpha", "zeta"]


# --- plain http stays allowed on an inline config (#340) ------------------------


async def test_plain_http_urls_are_allowed_on_an_inline_config() -> None:
    accepted = await _accepted(
        {
            "tools": [
                {
                    "name": "lookup",
                    "description": "Look it up",
                    "parameters_schema": {"type": "object"},
                    "webhook_url": "http://crm.example/lookup",
                }
            ],
            "mcp_servers": [
                {"name": "crm", "transport": "http", "url": "http://mcp.example/mcp"}
            ],
            "server_url": {"url": "http://events.example/hook"},
        }
    )

    assert accepted.config.tools[0].webhook_url == "http://crm.example/lookup"
    assert accepted.config.mcp_servers[0].url == "http://mcp.example/mcp"


# --- provider rules, with the API's messages ------------------------------------


@pytest.mark.parametrize(
    ("llm", "field", "reason"),
    [
        (
            {"provider": "xai", "base_url": "https://gw.example/v1"},
            "llm",
            "base_url is not supported for the xai provider",
        ),
        (
            {"provider": "xai", "reasoning_effort": "minimal"},
            "llm",
            "reasoning_effort 'minimal' is not an xAI value (low|medium|high)",
        ),
        (
            {
                "provider": "openai",
                "failover": [{"provider": "xai", "base_url": "https://gw.example"}],
            },
            "llm.failover[0]",
            "base_url is not supported for the xai provider",
        ),
    ],
)
async def test_an_xai_llm_rule_is_refused(
    llm: dict[str, Any], field: str, reason: str
) -> None:
    refusal = await _refused({"llm": llm})

    assert (refusal.field, refusal.reason) == (field, reason)


async def test_an_xai_llm_on_an_s2s_agent_that_builds_no_llm_is_not_refused() -> None:
    """Nothing builds it, so it fails nothing today (#474 story 29)."""
    await _accepted(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai"},
            "llm": {"provider": "xai", "base_url": "https://gw.example"},
        }
    )


async def test_an_xai_llm_as_gpt_live_1s_delegate_is_refused() -> None:
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai_live"},
            "tools": [
                {"name": "end_call", "description": "End", "parameters_schema": {}}
            ],
            "llm": {"provider": "xai", "reasoning_effort": "minimal"},
        }
    )

    assert refusal.field == "llm"


@pytest.mark.parametrize(
    ("tts", "field"),
    [
        ({"provider": "xai", "speed": 2.0}, "tts"),
        (
            {"provider": "deepgram", "failover": [{"provider": "xai", "speed": 0.5}]},
            "tts.failover[0]",
        ),
    ],
)
async def test_an_xai_tts_speed_out_of_range_is_refused(
    tts: dict[str, Any], field: str
) -> None:
    refusal = await _refused({"tts": tts})

    assert refusal.field == field
    assert refusal.reason == "tts.speed on xai must be between 0.7 and 1.5"


@pytest.mark.parametrize(
    ("s2s", "reason"),
    [
        (
            {"provider": "xai", "temperature": 0.5},
            "s2s.temperature is not supported for the xai S2S provider: Grok "
            "Voice's session has no temperature; remove s2s.temperature",
        ),
        (
            {"provider": "xai", "max_tokens": 200},
            "s2s.max_tokens is not supported for the xai S2S provider: Grok "
            "Voice's session has no output token limit; remove s2s.max_tokens",
        ),
    ],
)
async def test_a_grok_voice_sampling_field_is_refused(
    s2s: dict[str, Any], reason: str
) -> None:
    refusal = await _refused({"pipeline_mode": "s2s", "s2s": s2s})

    assert (refusal.field, refusal.reason) == ("s2s", reason)


async def test_a_grok_voice_base_url_is_refused_pointing_at_gateway_mode() -> None:
    refusal = await _refused(
        {"pipeline_mode": "s2s", "s2s": {"provider": "xai", "base_url": "wss://gw"}}
    )

    assert refusal.field == "s2s"
    assert "gateway mode" in refusal.reason


async def test_a_bad_grok_voice_extra_is_refused() -> None:
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "xai", "extra": {"reasoning": {"effort": "max"}}},
        }
    )

    assert refusal.reason.startswith("s2s.extra.reasoning is not a valid")


async def test_an_s2s_rule_on_a_cascade_agent_is_not_refused() -> None:
    """The build makes no realtime service for a cascade agent."""
    await _accepted({"s2s": {"provider": "xai", "temperature": 0.5}})


async def test_deepgram_flux_mixed_with_another_stt_is_refused() -> None:
    refusal = await _refused(
        {
            "stt": {
                "provider": "deepgram",
                "model": "flux-general-en",
                "failover": [{"provider": "deepgram", "model": "nova-3-general"}],
            }
        }
    )

    assert (refusal.field, refusal.reason) == ("stt.failover", MIXED_FLUX_GROUP)


async def test_an_eleven_realtime_session_longer_than_eleven_allows_is_refused() -> (
    None
):
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "elevenlabs"},
            "max_call_duration_seconds": 9000,
        }
    )

    assert refusal.field == "max_call_duration_seconds"
    assert "Eleven Realtime session" in refusal.reason


async def test_an_eleven_realtime_base_url_is_refused() -> None:
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "elevenlabs", "base_url": "wss://gw"},
        }
    )

    assert refusal.field == "s2s"


async def test_an_openai_gateway_aimed_at_elevenlabs_is_refused() -> None:
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai", "base_url": "wss://api.elevenlabs.io/x"},
        }
    )

    assert refusal.field == "s2s"
    assert "ElevenLabs" in refusal.reason


# --- the procedures graph rule --------------------------------------------------


async def test_a_broken_procedure_graph_is_refused_naming_procedure_and_step() -> None:
    refusal = await _refused({"procedures": [BROKEN_GRAPH]})

    expected = procedures_refusal(
        AgentConfig.model_validate({"procedures": [BROKEN_GRAPH]}).procedures
    )
    assert refusal.field == "procedures"
    assert refusal.reason == expected
    assert "procedures[0] 'billing'" in refusal.reason
    assert "'a'" in refusal.reason


async def test_an_unreadable_procedure_is_refused_with_the_domain_rules_words() -> None:
    refusal = await _refused({"procedures": [{"name": "x", "steps": "a, then b"}]})

    assert "cannot be read" in refusal.reason


async def test_a_step_tool_the_agent_lacks_is_not_refused_as_the_build_never_did() -> (
    None
):
    """The API's step-tool check (#447) is the schema's; the build skips it."""
    await _accepted(
        {
            "procedures": [
                {
                    "name": "p",
                    "steps": [
                        {
                            "id": "a",
                            "kind": "check",
                            "tool": "nowhere",
                            "start": True,
                            "moves": [{"to": "b"}],
                        },
                        {"id": "b", "kind": "end"},
                    ],
                }
            ]
        }
    )


# --- warnings an accepted S2S agent carries -------------------------------------


async def test_procedures_on_s2s_are_a_warning_not_a_refusal() -> None:
    accepted = await _accepted(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai"},
            "procedures": [BROKEN_GRAPH],
        }
    )

    assert _codes(accepted) == ["procedures_ignored_on_s2s"]


async def test_skills_and_auto_knowledge_on_s2s_are_warnings() -> None:
    accepted = await _accepted(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai"},
            "skills": [{"skill": "billing"}],
            "knowledge_bases": [
                {"knowledge_base_id": str(uuid.uuid4()), "mode": "auto"}
            ],
        }
    )

    assert sorted(_codes(accepted)) == [
        "knowledge_mode_unsupported",
        "skills_ignored_on_s2s",
    ]


# --- an agent id: one of the project's, or refused ------------------------------


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> dict[uuid.UUID, Any]:
    """An in-memory agent table, looked up the way the repository scopes it."""
    rows: dict[uuid.UUID, Any] = {}

    async def get_agent_by_id(_session: Any, agent_id: uuid.UUID, *, project_id=None):
        row = rows.get(agent_id)
        if row is None or (project_id is not None and row.project_id != project_id):
            return None
        return row

    async def kb_modes_by_agent(_session: Any, agent_ids: list[uuid.UUID]):
        return {
            agent_id: rows[agent_id].kb_modes
            for agent_id in agent_ids
            if rows[agent_id].kb_modes
        }

    monkeypatch.setattr(agent_acceptance.agent_repo, "get_agent_by_id", get_agent_by_id)
    monkeypatch.setattr(
        agent_acceptance.knowledge_repo, "kb_modes_by_agent", kb_modes_by_agent
    )
    return rows


def _row(project_id: uuid.UUID, config: dict[str, Any], kb_modes=()) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        config_blob=config,
        kb_modes=list(kb_modes),
    )


async def test_an_agent_id_in_the_project_is_accepted_with_its_config(agents) -> None:
    row = _row(PROJECT, {"system_prompt": "Stored"})
    agents[row.id] = row

    accepted = await _accept(row.id, session=object())

    assert isinstance(accepted, AcceptedAgent)
    assert accepted.agent_id == row.id
    assert accepted.config.system_prompt == "Stored"


async def test_another_projects_agent_id_is_refused(agents) -> None:
    row = _row(OTHER_PROJECT, {"system_prompt": "Theirs"})
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
    row = _row(
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
    row = _row(PROJECT, {"tts": {"provider": "xai", "speed": 3.0}})
    agents[row.id] = row

    refusal = await _accept(row.id, session=object())

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field == "tts"


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
    row = _row(PROJECT, {**GPT_LIVE_1, "llm": XAI_GATEWAY_LLM}, kb_modes=["tool"])
    agents[row.id] = row

    refusal = await _accept(row.id, origin=origin, session=object())

    assert isinstance(refusal, AgentRefusal), refusal
    assert (refusal.field, refusal.reason) == ("llm", XAI_GATEWAY_REASON)


async def test_a_linked_tool_knowledge_base_with_a_sound_delegate_is_accepted(
    agents,
) -> None:
    row = _row(
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
    row = _row(PROJECT, {**GPT_LIVE_1, "llm": XAI_GATEWAY_LLM}, kb_modes=modes)
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
