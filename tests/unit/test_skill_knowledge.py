"""A skill's knowledge bases (#429): what resolution reads, what a load adds
to the prompt and the tools, and what `query_knowledge` searches.

End to end in `tests/integration/test_harness_call_skill_knowledge.py`; the
load's apply step stays atomic in `test_handoff_switch_is_atomic.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.orchestrator.skill_loader import CallSkills, prepare_load
from turncall.orchestrator.tool_bridge import SwitchInputs
from turncall.services.retrieval import (
    KNOWLEDGE_AWARENESS_HINT,
    KNOWLEDGE_TOOL_HINT,
)
from turncall.services.skill_knowledge import (
    NO_KNOWLEDGE,
    SkillKnowledge,
    auto_mode_warning,
    resolve_skill_knowledge,
)
from turncall.services.skills import ResolvedSkill, loaded_section

pytestmark = pytest.mark.unit


@dataclass
class _Params:
    """pipecat's FunctionCallParams, the parts the handler reads (a dataclass:
    `recorded_tool` replaces its callback)."""

    arguments: dict
    result_callback: Any


AGENT = AgentConfig(system_prompt="You are Maya.")


def _search(**extra) -> dict:
    return {"knowledge_base_id": uuid4(), "mode": "tool", **extra}


def _skill(name: str, knowledge: SkillKnowledge = NO_KNOWLEDGE) -> ResolvedSkill:
    return ResolvedSkill(
        id=uuid4(),
        name=name,
        version=1,
        description=f"When {name}.",
        instructions=f"Do {name}.",
        tools=(),
        knowledge=knowledge,
    )


def _context(mcp=None):
    return SimpleNamespace(
        call_id=uuid4(),
        project_id=uuid4(),
        session_factory=MagicMock(),
        mcp_manager=mcp,
        tool_mocks=None,
        records=False,
    )


def _names(prepared) -> list[str]:
    return [f.name for f in prepared.frames[1].tools.standard_tools]


def _prompt(prepared) -> str:
    return prepared.frames[0].delta.system_instruction


def test_prompt_mode_documents_follow_the_skills_instructions():
    skill = _skill("explain_bill", SkillKnowledge(text="[Knowledge Base Context] x"))
    assert loaded_section(skill) == (
        "## Skill: explain_bill (v1)\nDo explain_bill.\n\n[Knowledge Base Context] x"
    )
    assert loaded_section(_skill("plain")) == "## Skill: plain (v1)\nDo plain."


def test_a_load_adds_query_knowledge_and_its_hints_for_an_agent_without_one():
    bill = _skill("explain_bill", SkillKnowledge(search=(_search(),)))
    skills = CallSkills(base=AGENT, available=(bill,), switch=SwitchInputs())

    prepared = prepare_load(skills, bill, _context())

    assert prepared.knowledge is not None
    assert _names(prepared) == ["load_skill", "query_knowledge"]
    assert KNOWLEDGE_TOOL_HINT in _prompt(prepared)
    assert KNOWLEDGE_AWARENESS_HINT in _prompt(prepared)
    # Before the load: neither.
    assert "query_knowledge" not in [t.name for t in skills.config().tools]


def test_the_agents_own_query_knowledge_covers_the_skill_and_the_hints_stay_once():
    from turncall.orchestrator.knowledge_processor import knowledge_tool_definition

    own = _search(tool_description="Search the store FAQ")
    preamble = f"{KNOWLEDGE_AWARENESS_HINT}\n\n{KNOWLEDGE_TOOL_HINT}"
    switch = SwitchInputs(
        knowledge_preamble=preamble,
        extra_tools=(knowledge_tool_definition([own]),),
    )
    bill = _skill("explain_bill", SkillKnowledge(search=(_search(),)))
    skills = CallSkills(
        base=AGENT, available=(bill,), switch=switch, own_knowledge=(own,)
    )

    prepared = prepare_load(skills, bill, _context())

    assert _names(prepared).count("query_knowledge") == 1
    assert _prompt(prepared).count(KNOWLEDGE_TOOL_HINT) == 1
    assert prepared.knowledge is not None  # the one handler, over both


def test_searched_is_the_agents_then_the_loaded_skills_each_kb_once():
    own = _search()
    shared = _search()
    bill = _skill("explain_bill", SkillKnowledge(search=(shared, _search())))
    skills = CallSkills(
        base=AGENT,
        available=(bill,),
        switch=SwitchInputs(),
        own_knowledge=(own, shared),
    )
    assert [a["knowledge_base_id"] for a in skills.searched()] == [
        own["knowledge_base_id"],
        shared["knowledge_base_id"],
    ]
    skills.loaded = (bill,)
    assert len(skills.searched()) == 3
    skills.loaded = ()  # unloaded: searched no more
    assert len(skills.searched()) == 2


@pytest.mark.asyncio
async def test_the_handler_searches_what_is_loaded_when_it_is_called():
    bill = _skill("explain_bill", SkillKnowledge(search=(_search(top_k=2),)))
    skills = CallSkills(base=AGENT, available=(bill,), switch=SwitchInputs())
    context = _context()
    context.session_factory = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=MagicMock()), __aexit__=AsyncMock()
        )
    )
    prepared = prepare_load(skills, bill, context)
    params = _Params(arguments={"query": "roaming"}, result_callback=AsyncMock())
    found = {"results": "x", "count": 1}
    with patch(
        "turncall.orchestrator.knowledge_processor.query_knowledge",
        AsyncMock(return_value=found),
    ) as search:
        await prepared.knowledge.handler(params)  # prepared, not applied
        skills.loaded = prepared.loaded
        await prepared.knowledge.handler(params)

    first, second = search.await_args_list
    assert first.kwargs["knowledge_base_ids"] == []
    assert second.kwargs["knowledge_base_ids"] == [
        bill.knowledge.search[0]["knowledge_base_id"]
    ]
    assert second.kwargs["top_k"] == 2


def test_a_tool_named_query_knowledge_keeps_the_name():
    bill = _skill("explain_bill", SkillKnowledge(search=(_search(),)))
    agent = AgentConfig(
        system_prompt="You are Maya.",
        tools=[
            ToolDefinition(
                name="query_knowledge",
                description="The agent's own search",
                parameters_schema={"type": "object", "properties": {}},
                webhook_url="http://agent/search",
            )
        ],
    )
    skills = CallSkills(base=agent, available=(bill,), switch=SwitchInputs())
    assert prepare_load(skills, bill, _context()).knowledge is None

    mcp = MagicMock(is_mcp_tool=MagicMock(return_value=True))
    skills = CallSkills(base=AGENT, available=(bill,), switch=SwitchInputs())
    prepared = prepare_load(skills, bill, _context(mcp))
    assert prepared.knowledge is None
    assert KNOWLEDGE_TOOL_HINT not in _prompt(prepared)


def test_auto_mode_is_a_warning_naming_the_knowledge_bases():
    auto, default = str(uuid4()), str(uuid4())
    warning = auto_mode_warning(
        [
            {"knowledge_base_id": auto, "mode": "auto"},
            {"knowledge_base_id": str(uuid4()), "mode": "tool"},
            {"knowledge_base_id": default},
        ]
    )
    assert warning is not None
    assert warning["code"] == "knowledge_mode_unsupported"
    assert warning["knowledge_base_ids"] == [auto, default]
    assert auto_mode_warning([{"knowledge_base_id": auto, "mode": "prompt"}]) is None
    assert auto_mode_warning([]) is None


@pytest.mark.asyncio
async def test_resolution_reads_prompt_text_keeps_tool_and_skips_auto_and_strangers():
    prompt_kb, tool_kb, auto_kb, stranger = uuid4(), uuid4(), uuid4(), uuid4()
    project = uuid4()

    async def get_kb(_session, kb_id, *, project_id):
        assert project_id == project
        return None if kb_id == stranger else SimpleNamespace(id=kb_id)

    text = AsyncMock(return_value="[Knowledge Base Context] fees")
    with (
        patch(
            "turncall.storage.repositories.knowledge_repo.get_knowledge_base_by_id",
            get_kb,
        ),
        patch("turncall.services.retrieval.get_full_text_context", text),
    ):
        knowledge = await resolve_skill_knowledge(
            MagicMock(),
            project,
            "explain_bill",
            [
                {"knowledge_base_id": str(prompt_kb), "mode": "prompt"},
                {"knowledge_base_id": str(tool_kb), "mode": "tool", "top_k": 2},
                {"knowledge_base_id": str(auto_kb), "mode": "auto"},
                {"knowledge_base_id": str(stranger), "mode": "tool"},
                {"knowledge_base_id": "not-a-uuid"},
            ],
        )

    assert knowledge.text == "[Knowledge Base Context] fees"
    assert text.await_args.args[1] == [prompt_kb]
    assert [a["knowledge_base_id"] for a in knowledge.search] == [tool_kb]
    assert knowledge.search[0]["top_k"] == 2


@pytest.mark.asyncio
async def test_a_skill_with_no_knowledge_bases_reads_nothing():
    session = MagicMock()
    knowledge = await resolve_skill_knowledge(session, uuid4(), "x", [])
    assert knowledge == SkillKnowledge()
    session.execute.assert_not_called()
