"""Skills on a voice call (#424): the precedence rule, resolution, and the
`load_skill` handler.

Precedence is built-in, then the agent's own tools, then loaded skills' tools
in load order, then MCP — and what the model is shown and what runs must
agree, so both are checked against one config. The end-to-end behaviour is in
`tests/integration/test_harness_call_skills.py`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, SkillRef, ToolDefinition
from turncall.services.skill_library import SkillSource
from turncall.services.skills import (
    ResolvedSkill,
    _as_resolved,
    resolve_skills,
    skilled_config,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _own_skills():
    """Every project here reads its own skills: the skill library (#432) is
    covered over the running app."""

    async def own(_session, project_id):
        return SkillSource(project_id)

    with patch("turncall.services.skills.skill_source", new=own):
        yield


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


@pytest.mark.asyncio
async def test_a_pin_resolves_to_its_version_whatever_is_live():
    """#426: a pinned entry runs exactly its version — an archived one too —
    and never the live one; a pin that names a draft, another skill's
    version or nothing in the project is a warning and a skipped skill."""
    project = uuid4()

    def _row(name, version, state):
        return SimpleNamespace(
            id=uuid4(),
            name=name,
            version=version,
            state=state,
            description="d",
            instructions=f"{name} v{version}",
            tools=[],
        )

    live = _row("verify", 3, "published")
    archived = _row("verify", 1, "archived")
    draft = _row("explain", 2, "draft")
    other = _row("promise", 1, "published")
    rows = {r.id: r for r in (archived, draft, other)}

    async def get_by_id(_session, skill_id, *, project_id):
        assert project_id == project
        return rows.get(skill_id)

    async def get_published(_session, _project, name):
        return live if name == "verify" else None

    with (
        patch("turncall.storage.repositories.skill_repo.get_by_id", new=get_by_id),
        patch(
            "turncall.storage.repositories.skill_repo.get_published",
            new=get_published,
        ),
    ):
        pinned = await resolve_skills(
            MagicMock(),
            project,
            [
                SkillRef(skill="verify", version_id=archived.id),
                SkillRef(skill="explain", version_id=draft.id),
                SkillRef(skill="nope", version_id=other.id),
                SkillRef(skill="gone", version_id=uuid4()),
            ],
            variables={},
            taken_tool_names=set(),
        )
        following = await resolve_skills(
            MagicMock(),
            project,
            [SkillRef(skill="verify")],
            variables={},
            taken_tool_names=set(),
        )

    assert [(s.name, s.version, s.pinned) for s in pinned] == [("verify", 1, True)]
    assert [(s.name, s.version, s.pinned) for s in following] == [("verify", 3, False)]


def test_why_a_pin_cannot_run():
    from turncall.services.skills import pin_refusal

    def row(name="verify", state="published", version=2):
        return SimpleNamespace(name=name, state=state, version=version)

    assert pin_refusal("verify", row()) is None
    assert pin_refusal("verify", row(state="archived")) is None
    assert "not a version of a skill in this project" in pin_refusal("verify", None)
    assert "a version of 'explain', not of 'verify'" in pin_refusal(
        "verify", row(name="explain")
    )
    assert "is a draft (v2)" in pin_refusal("verify", row(state="draft"))


def test_an_unusable_skills_entry_drops_that_skill_not_the_config():
    config = AgentConfig.model_validate(
        {"skills": [{"skill": "ok"}, {"nope": 1}, "bad"]}
    )
    assert config.skills == [SkillRef(skill="ok")]
    assert AgentConfig.model_validate({"skills": "x"}).skills == []


# --- load_skill ------------------------------------------------------------


def _call(skills=None, *, records=False):
    return SimpleNamespace(
        call_id=uuid4(),
        project_id=uuid4(),
        skills=skills,
        records=records,
        is_eval=not records,
        session_factory=MagicMock(),
    )


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


# --- the record (#425) -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("records", [True, False])
async def test_a_load_is_a_skill_loaded_event_only_on_a_recorded_call(records):
    """A load is recorded (stored and dispatched) on a call with a row, after
    the switch; an eval (`records` false) records nothing."""
    from turncall.orchestrator.skill_loader import CallSkills, run_load_skill
    from turncall.orchestrator.tool_bridge import SwitchInputs

    skill = _skill("explain_bill", version=3)
    skills = CallSkills(AGENT, (skill,), SwitchInputs())
    call = _call(skills, records=records)
    _, params = _params()
    emitted: list[dict] = []

    async def emit(_session, **kwargs):
        emitted.append(kwargs)

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    call.session_factory = MagicMock(return_value=session)
    with (
        patch("turncall.orchestrator.tool_bridge.register_tools"),
        patch("turncall.events.emit.emit_call_event", emit),
    ):
        await run_load_skill({"name": "explain_bill"}, call, params)
        from turncall.orchestrator.tool_bridge import _BG_TASKS

        await asyncio.gather(*_BG_TASKS)

    if not records:
        assert emitted == []
        call.session_factory.assert_not_called()
        return
    [event] = emitted
    assert event["event_type"] == "skill.loaded"
    assert event["payload"] == {
        "skill": "explain_bill",
        "version": 3,
        "reason": "load_skill",
    }
    assert event["call_id"] == call.call_id and event["commit"] is True


@pytest.mark.asyncio
async def test_the_resolution_is_saved_only_on_a_recorded_call():
    from turncall.orchestrator.skill_loader import record_resolution

    skill = _skill("explain_bill", version=3)
    eval_call = _call(records=False)
    await record_resolution(eval_call, (skill,))
    eval_call.session_factory.assert_not_called()

    call = _call(records=True)
    session = MagicMock(commit=AsyncMock())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    call.session_factory = MagicMock(return_value=session)
    with patch(
        "turncall.storage.repositories.call_repo.append_call_metadata_list",
        AsyncMock(),
    ) as append:
        await record_resolution(call, (skill,))

    # Appended, so a handoff's target's resolution joins the first agent's
    # (#427); a version already there is skipped by its id.
    append.assert_awaited_once_with(
        session,
        call.call_id,
        "resolved_skills",
        [
            {
                "id": str(skill.id),
                "skill": "explain_bill",
                "version": 3,
                "pinned": False,
            }
        ],
        unique_by="id",
    )
    session.commit.assert_awaited_once()


# --- the limit, the no-op, a handoff (#427) ----------------------------------


def _holding(*loaded: ResolvedSkill, available=(), cap: int = 3):
    from turncall.orchestrator.skill_loader import CallSkills
    from turncall.orchestrator.tool_bridge import SwitchInputs

    base = AGENT.model_copy(update={"max_loaded_skills": cap})
    return CallSkills(base, (*loaded, *available), SwitchInputs(), loaded=tuple(loaded))


def test_a_load_past_the_cap_unloads_the_oldest_in_the_same_config():
    from turncall.orchestrator.skill_loader import prepare_load

    a, b, c, d = (_skill(n, _tool(f"{n}_tool")) for n in "abcd")
    skills = _holding(a, b, c, available=(d,))

    prepared = prepare_load(skills, d)

    assert prepared.unloaded == (a,)
    assert prepared.loaded == (b, c, d)
    prompt = prepared.config.system_prompt
    assert "## Skill: a" not in prompt
    assert all(f"## Skill: {n} (v1)" in prompt for n in "bcd")
    # Still loadable again: the list names every skill.
    assert "- a: When a is needed." in prompt
    assert [t.name for t in prepared.config.tools] == [
        "load_skill",
        "end_call",
        "lookup",
        "b_tool",
        "c_tool",
        "d_tool",
    ]
    # One switch: the frames carry the unload and the load together.
    prompt_frame, tools_frame = prepared.frames
    assert "## Skill: a" not in prompt_frame.delta.system_instruction
    assert "a_tool" not in [t.name for t in tools_frame.tools.standard_tools]
    assert skills.loaded == (a, b, c), "preparing applied nothing"


def test_a_cap_of_one_swaps_the_loaded_skill():
    from turncall.orchestrator.skill_loader import prepare_load

    a, b = _skill("a"), _skill("b")
    prepared = prepare_load(_holding(a, available=(b,), cap=1), b)

    assert (prepared.unloaded, prepared.loaded) == ((a,), (b,))


@pytest.mark.asyncio
async def test_loading_a_loaded_skill_changes_nothing():
    from turncall.orchestrator.skill_loader import run_load_skill

    a = _skill("a", _tool("a_tool"))
    skills = _holding(a)
    call = _call(skills, records=True)
    queued, params = _params()
    with (
        patch("turncall.orchestrator.tool_bridge.register_tools") as register,
        patch("turncall.orchestrator.skill_loader.record_skill_events") as record,
    ):
        result = json.loads(await run_load_skill({"name": "a"}, call, params))

    assert result["success"] is True
    assert "already loaded" in result["message"]
    assert result["details"] == {"skill": "a", "version": 1, "already_loaded": True}
    assert queued == [] and skills.loaded == (a,)
    register.assert_not_called()
    record.assert_not_called()
    # An ordinary result: the model's next inference runs after it, as usual.
    assert "properties" not in params.result_callback.await_args.kwargs


@pytest.mark.asyncio
async def test_an_unload_is_recorded_before_the_load_that_made_room():
    from turncall.orchestrator.skill_loader import run_load_skill

    a, b = _skill("a", version=4), _skill("b", version=2)
    skills = _holding(a, available=(b,), cap=1)
    call = _call(skills, records=True)
    _, params = _params()
    emitted: list[dict] = []

    async def emit(_session, **kwargs):
        emitted.append(kwargs)

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    call.session_factory = MagicMock(return_value=session)
    with (
        patch("turncall.orchestrator.tool_bridge.register_tools"),
        patch("turncall.events.emit.emit_call_event", emit),
    ):
        result = json.loads(await run_load_skill({"name": "b"}, call, params))
        from turncall.orchestrator.tool_bridge import _BG_TASKS

        await asyncio.gather(*_BG_TASKS)

    assert skills.loaded == (b,)
    assert result["details"] == {"skill": "b", "version": 2, "unloaded": ["a"]}
    assert "'a' was unloaded" in result["message"]
    assert [(e["event_type"], e["payload"]) for e in emitted] == [
        ("skill.unloaded", {"skill": "a", "version": 4, "reason": "limit"}),
        ("skill.loaded", {"skill": "b", "version": 2, "reason": "load_skill"}),
    ]


@pytest.mark.asyncio
async def test_a_handoff_target_without_skills_runs_its_own_config():
    from turncall.orchestrator.skill_loader import prepare_handoff_skills

    target = AgentConfig(system_prompt="You are billing.")
    skills, config = await prepare_handoff_skills(target, _call())

    assert config is target
    assert skills.available == () and skills.loaded == ()


@pytest.mark.asyncio
async def test_a_handoff_target_with_skills_gets_its_list_and_load_skill():
    from turncall.orchestrator.skill_loader import prepare_handoff_skills

    target = AgentConfig(
        system_prompt="You are billing.",
        skills=[SkillRef(skill="verify")],
        max_loaded_skills=2,
    )
    verify = _skill("verify", _tool("verify_customer"))
    with patch(
        "turncall.orchestrator.skill_loader.resolve_call_skills",
        AsyncMock(return_value=(verify,)),
    ) as resolve:
        skills, config = await prepare_handoff_skills(target, call := _call())

    resolve.assert_awaited_once_with(target, call)
    assert skills.available == (verify,) and skills.loaded == ()
    assert skills.base is target and skills.cap == 2
    assert "- verify: When verify is needed." in config.system_prompt
    assert "## Skill:" not in config.system_prompt
    assert [t.name for t in config.tools] == ["load_skill"]


@pytest.mark.asyncio
async def test_after_a_handoff_to_an_agent_without_skills_a_stale_load_applies_nothing():
    """The first agent's `load_skill` handler stays registered (pipecat's
    registry is never cleared). Called anyway, it finds the target's skills —
    none — and switches nothing back to the first agent's config."""
    from turncall.orchestrator.skill_loader import no_skills, run_load_skill

    a = _skill("a", _tool("a_tool"))
    skills = _holding(a)
    skills.adopt(no_skills(AgentConfig(system_prompt="You are billing.")))
    queued, params = _params()
    with patch("turncall.orchestrator.tool_bridge.register_tools") as register:
        result = json.loads(await run_load_skill({"name": "a"}, _call(skills), params))

    assert result["success"] is False
    assert result["message"] == "There is no skill named 'a' to load."
    assert queued == [] and skills.loaded == ()
    register.assert_not_called()


@pytest.mark.asyncio
async def test_a_handoff_records_the_targets_resolution_only_once_switched():
    from turncall.orchestrator import tool_bridge
    from turncall.orchestrator.skill_loader import CallSkills
    from turncall.orchestrator.tool_bridge import (
        PreparedHandoff,
        SwitchInputs,
        build_switch_frames,
    )

    verify = _skill("verify")
    target = CallSkills(AGENT, (verify,), SwitchInputs())
    prepared = PreparedHandoff(
        "billing", target.config(), build_switch_frames(target.config()), target
    )
    recorded = AsyncMock()

    for committed, expected in ((False, 0), (True, 1)):
        recorded.reset_mock()
        with (
            patch.object(
                tool_bridge, "_prepare_handoff", AsyncMock(return_value=prepared)
            ),
            patch.object(
                tool_bridge,
                "_execute_builtin",
                AsyncMock(return_value=json.dumps({"success": committed})),
            ),
            patch.object(tool_bridge, "_apply_handoff_context", AsyncMock()),
            patch("turncall.orchestrator.skill_loader.record_resolution", recorded),
        ):
            await tool_bridge.run_handoff({"agent_id": "x"}, _call(), MagicMock())
            await asyncio.gather(*tool_bridge._BG_TASKS)
        assert recorded.await_count == expected
    assert recorded.await_args.args[1] == (verify,)


def test_max_loaded_skills_defaults_to_three_and_an_inline_one_never_fails():
    assert AgentConfig().max_loaded_skills == 3
    assert AgentConfig.model_validate({"max_loaded_skills": 0}).max_loaded_skills == 1
    assert AgentConfig.model_validate({"max_loaded_skills": 99}).max_loaded_skills == 10
    assert AgentConfig.model_validate({"max_loaded_skills": "x"}).max_loaded_skills == 3


@pytest.mark.parametrize("value", [0, 11, -1])
def test_the_api_refuses_a_cap_out_of_range(value):
    from pydantic import ValidationError

    from turncall.api.v1.schemas.agents import AgentConfigSchema

    with pytest.raises(ValidationError, match="max_loaded_skills"):
        AgentConfigSchema(max_loaded_skills=value)
    assert AgentConfigSchema(max_loaded_skills=10).max_loaded_skills == 10
