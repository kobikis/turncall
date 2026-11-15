"""Agent acceptance (#474, #476): whether an agent config can run.

Every case sends a config in through `accept_agent` and reads what comes out
— an accepted agent with these warnings, or a refusal naming this field.
The policy table, an inline config, unknown fields, the procedures rule and
an S2S agent's warnings; provider rules and stored agents have their own
files.
"""

import uuid

import pytest

from tests.acceptance_support import (
    CALL_INIT,
)
from tests.acceptance_support import accepted as _accepted
from tests.acceptance_support import codes as _codes
from tests.acceptance_support import refused as _refused
from turncall.domain.models import AgentConfig
from turncall.domain.procedure_graph import procedures_refusal
from turncall.services.agent_acceptance import (
    POLICIES,
    AgentOrigin,
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
