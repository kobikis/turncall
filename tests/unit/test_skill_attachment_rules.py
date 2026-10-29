"""What an agent may list as skills (#428), the pure rules.

A skill tool may not take a name already taken by a built-in, the agent's own
tool or an earlier listed skill's tool, and the refusal names both sides. An
agent may not list `load_skill` among its own tools. An S2S agent's skills are
a warning on the agent read, and the build skips them on the same rule,
logging the same message.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from loguru import logger
from pydantic import ValidationError

from turncall.domain.models import AgentConfig, SkillRef
from turncall.orchestrator.config_warnings import (
    config_warnings,
    skills_on_s2s_warning,
)
from turncall.services.skills import Attached, tool_collision

pytestmark = pytest.mark.unit


def _attached(index: int, name: str, *tools: str) -> Attached:
    return Attached(index=index, name=name, tool_names=tools)


def test_skills_whose_tools_are_free_are_fine():
    attached = [
        _attached(0, "verify_identity", "verify_customer"),
        _attached(1, "explain_bill", "get_invoices"),
    ]
    assert tool_collision(["lookup_account", "end_call"], attached) is None


def test_a_skill_tool_named_like_the_agents_own_names_both():
    refusal = tool_collision(
        ["get_invoices"],
        [_attached(0, "a", "x"), _attached(1, "explain_bill", "get_invoices")],
    )

    assert refusal is not None
    assert refusal.message == (
        "skills[1] 'explain_bill' tool 'get_invoices' collides with the "
        "agent's own tool 'get_invoices'"
    )
    assert refusal.details == {
        "field": "skills[1]",
        "skill": "explain_bill",
        "tool": "get_invoices",
        "collides_with": "the agent's own tool 'get_invoices'",
    }


def test_two_skills_sharing_a_tool_name_both_named():
    refusal = tool_collision(
        [],
        [
            _attached(0, "explain_bill", "get_invoices"),
            _attached(1, "payment_promise", "get_invoices"),
        ],
    )

    assert refusal is not None
    assert refusal.message == (
        "skills[1] 'payment_promise' tool 'get_invoices' collides with "
        "skills[0] 'explain_bill' tool 'get_invoices'"
    )


def test_a_built_in_name_is_named_as_the_built_in():
    refusal = tool_collision(["end_call"], [_attached(0, "old", "end_call")])

    assert refusal is not None
    assert refusal.message.endswith("collides with the built-in 'end_call'")


def test_the_agent_may_not_list_load_skill_itself():
    from turncall.api.v1.schemas.agents import AgentConfigSchema

    with pytest.raises(ValidationError, match="load_skill"):
        AgentConfigSchema.model_validate(
            {"tools": [{"name": "load_skill", "description": "Load one"}]}
        )
    assert AgentConfigSchema.model_validate(
        {"tools": [{"name": "end_call", "description": "End"}]}
    ).tools


def test_an_s2s_agent_with_skills_is_warned_and_a_cascade_one_is_not():
    s2s = AgentConfig(pipeline_mode="s2s", skills=[SkillRef(skill="explain_bill")])

    warning = skills_on_s2s_warning(s2s)
    assert warning is not None
    assert warning["code"] == "skills_ignored_on_s2s"
    assert warning["skills"] == ["explain_bill"]
    assert warning["provider"] == s2s.s2s.provider
    assert "explain_bill" in warning["message"]
    assert warning in config_warnings(s2s)

    assert skills_on_s2s_warning(AgentConfig(pipeline_mode="s2s")) is None
    cascade = AgentConfig(skills=[SkillRef(skill="explain_bill")])
    assert skills_on_s2s_warning(cascade) is None


async def test_an_s2s_build_skips_the_skills_logging_the_warning():
    from turncall.orchestrator.skill_loader import resolve_call_skills

    config = AgentConfig(pipeline_mode="s2s", skills=[SkillRef(skill="explain_bill")])
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m), level="WARNING", format="{message}")
    try:
        resolved = await resolve_call_skills(
            config,
            SimpleNamespace(has_database=True),  # type: ignore[arg-type]
        )
    finally:
        logger.remove(sink)

    assert resolved == ()
    message = skills_on_s2s_warning(config)["message"]  # type: ignore[index]
    assert any(m.strip() == f"skills_skipped: {message}" for m in lines), lines
