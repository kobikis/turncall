"""Skills on a voice call (#424): the precedence rule, resolution, and the
`load_skill` handler.

Precedence is built-in, then the agent's own tools, then loaded skills' tools
in load order, then MCP — and what the model is shown and what runs must
agree, so both are checked against one config. The end-to-end behaviour is in
`tests/integration/test_harness_call_skills.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, SkillRef, ToolDefinition
from turncall.services.skills import (
    ResolvedSkill,
    _as_resolved,
    resolve_skills,
    skilled_config,
)

pytestmark = pytest.mark.unit


def _tool(name: str, url: str = "http://x/hook", description: str = "d"):
    return ToolDefinition(
        name=name,
        description=description,
        parameters_schema={"type": "object", "properties": {}},
        webhook_url=url,
    )


def _skill(name: str, *tools: ToolDefinition, version: int = 1) -> ResolvedSkill:
    return ResolvedSkill(
        id=uuid4(),
        name=name,
        version=version,
        description=f"When {name} is needed.",
        instructions=f"Do {name}.",
        tools=tools,
    )


AGENT = AgentConfig(
    system_prompt="You are Maya.",
    tools=[
        ToolDefinition(name="end_call", description="End", parameters_schema={}),
        _tool("lookup", "http://agent/lookup"),
    ],
)


def test_no_skills_no_change():
    assert skilled_config(AGENT, ()) is AGENT


def test_the_list_and_load_skill_before_any_load():
    a, b = _skill("explain_bill", _tool("get_invoices")), _skill("verify")
    config = skilled_config(AGENT, (a, b))

    assert config.system_prompt == (
        "You are Maya.\n\n"
        "## Skills you can load\n"
        "Load a skill with load_skill when the conversation needs it. Its "
        "instructions and tools are added when you load it.\n"
        "- explain_bill: When explain_bill is needed.\n"
        "- verify: When verify is needed."
    )
    assert [t.name for t in config.tools] == ["load_skill", "end_call", "lookup"]
    [load] = [t for t in config.tools if t.name == "load_skill"]
    assert load.parameters_schema["properties"]["name"]["enum"] == [
        "explain_bill",
        "verify",
    ]
    assert load.parameters_schema["required"] == ["name"]


def test_precedence_is_the_same_where_advertised_and_where_registered():
    from turncall.orchestrator.pipeline_factory import _build_tools_schema
    from turncall.orchestrator.tool_bridge import register_tools

    first = _skill("first", _tool("shared", "http://first/shared"), _tool("x"))
    second = _skill("second", _tool("shared", "http://second/shared"), _tool("y"))
    # Loaded second-then-first: load order, not the agent's listing, decides.
    config = skilled_config(AGENT, (first, second), (second, first))
    mcp = [_tool("shared", "http://mcp/shared"), _tool("z"), _tool("lookup")]

    advertised = [t.name for t in _build_tools_schema(config, mcp).standard_tools]
    assert advertised == ["load_skill", "end_call", "lookup", "shared", "y", "x", "z"]

    registered: dict[str, ToolDefinition] = {}
    with patch(
        "turncall.orchestrator.tool_bridge._register_single_tool",
        new=lambda _llm, tool, *_a: registered.setdefault(tool.name, tool),
    ):
        register_tools(MagicMock(), [*config.tools, *mcp], SimpleNamespace())

    assert list(registered) == advertised
    assert registered["shared"].webhook_url == "http://second/shared"
    assert registered["lookup"].webhook_url == "http://agent/lookup"


def test_a_loaded_skill_adds_its_section_in_load_order():
    a, b = _skill("a", version=3), _skill("b", version=1)
    prompt = skilled_config(AGENT, (a, b), (b, a)).system_prompt

    assert prompt.endswith("## Skill: b (v1)\nDo b.\n\n## Skill: a (v3)\nDo a.")


def test_resolution_renders_variables_and_drops_a_tool_the_agent_owns():
    row = SimpleNamespace(
        id=uuid4(),
        name="explain_bill",
        version=4,
        description="When the bill changed.",
        instructions="Greet {{name}}; keep {{unknown}}.",
        tools=[
            _tool("lookup").model_dump(),
            _tool("get_invoices", description="{{name}}'s invoices").model_dump(),
        ],
    )
    skill = _as_resolved(row, {"name": "Dana"}, {"lookup", "end_call"})

    assert skill.instructions == "Greet Dana; keep {{unknown}}."
    assert [(t.name, t.description) for t in skill.tools] == [
        ("get_invoices", "Dana's invoices")
    ]
    assert skill.version == 4


@pytest.mark.asyncio
async def test_a_skill_that_does_not_resolve_is_skipped():
    live = SimpleNamespace(
        id=uuid4(),
        name="live",
        version=2,
        description="d",
        instructions="i",
        tools=[],
    )

    async def get_published(_session, _project, name):
        if name == "broken":
            raise RuntimeError("unreadable")
        return live if name == "live" else None

    with patch(
        "turncall.storage.repositories.skill_repo.get_published", new=get_published
    ):
        resolved = await resolve_skills(
            MagicMock(),
            uuid4(),
            [SkillRef(skill=n) for n in ("missing", "live", "broken", "live")],
            variables={},
            taken_tool_names=set(),
        )

    assert [(s.name, s.version) for s in resolved] == [("live", 2)]


def test_an_unusable_skills_entry_drops_that_skill_not_the_config():
    config = AgentConfig.model_validate(
        {"skills": [{"skill": "ok"}, {"nope": 1}, "bad"]}
    )
    assert config.skills == [SkillRef(skill="ok")]
    assert AgentConfig.model_validate({"skills": "x"}).skills == []


# --- load_skill ------------------------------------------------------------


def _call(skills=None):
    return SimpleNamespace(call_id=uuid4(), project_id=uuid4(), skills=skills)


def _params():
    queued: list = []

    async def queue_frame(frame):
        queued.append(frame)

    return queued, SimpleNamespace(
        llm=MagicMock(),
        context=MagicMock(),
        pipeline_worker=SimpleNamespace(queue_frame=queue_frame),
        result_callback=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_an_unknown_skill_is_said_and_nothing_is_applied():
    from turncall.orchestrator.skill_loader import CallSkills, run_load_skill
    from turncall.orchestrator.tool_bridge import SwitchInputs

    skills = CallSkills(AGENT, (_skill("verify"),), SwitchInputs())
    queued, params = _params()
    result = json.loads(
        await run_load_skill({"name": "billing"}, _call(skills), params)
    )

    assert result["success"] is False
    assert "billing" in result["message"] and "verify" in result["message"]
    assert queued == [] and skills.loaded == ()
    params.result_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_load_keeps_the_calls_preamble_and_discovered_tools_and_reruns_after():
    from pipecat.frames.frames import LLMRunFrame

    from turncall.orchestrator.skill_loader import CallSkills, run_load_skill
    from turncall.orchestrator.tool_bridge import SwitchInputs

    skill = _skill("explain_bill", _tool("get_invoices"))
    skills = CallSkills(
        AGENT,
        (skill,),
        SwitchInputs(knowledge_preamble="FAQ text.", extra_tools=(_tool("mcp_one"),)),
    )
    queued, params = _params()
    with patch("turncall.orchestrator.tool_bridge.register_tools") as register:
        result = json.loads(
            await run_load_skill({"name": "explain_bill"}, _call(skills), params)
        )

    assert result["success"] is True
    assert result["details"] == {"skill": "explain_bill", "version": 1}
    assert skills.loaded == (skill,)
    params.context.set_messages.assert_not_called()  # the conversation is kept

    prompt_frame, tools_frame = queued
    prompt = prompt_frame.delta.system_instruction
    assert prompt.startswith("FAQ text.\n\nYou are Maya.")
    assert prompt.endswith("## Skill: explain_bill (v1)\nDo explain_bill.")
    assert [t.name for t in tools_frame.tools.standard_tools] == [
        "load_skill",
        "end_call",
        "lookup",
        "get_invoices",
        "mcp_one",
    ]
    assert "get_invoices" in [t.name for t in register.call_args.args[1]]

    # The model's next inference is asked for behind the switch, not by the
    # result: the result runs no LLM, and once it is in the context an
    # LLMRunFrame is queued after both frames.
    properties = params.result_callback.await_args.kwargs["properties"]
    assert properties.run_llm is False
    await properties.on_context_updated()
    assert isinstance(queued[-1], LLMRunFrame) and len(queued) == 3
