"""Skills on a text turn (#430): what `load_skill` does to the turn's tools
and prompt, by the rules a call follows.

The answers, the cap and the already-loaded no-op are `services/skills`'s,
shared with the voice path; what is text's own is that a load changes the
tools the turn offers from the next round on (`ChatTools.current`), and what a
stored `loaded_skills` list holds when it is read back.
"""

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.services.chat_tools import build_chat_tools
from turncall.services.skill_knowledge import SkillKnowledge
from turncall.services.skills import ResolvedSkill
from turncall.services.text_skills import SessionSkills, _held

pytestmark = pytest.mark.unit


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url=f"https://x.test/{name}",
    )


def _skill(name: str, *tools: str, search: tuple = ()) -> ResolvedSkill:
    return ResolvedSkill(
        id=uuid4(),
        name=name,
        version=1,
        description=f"Use for {name}.",
        instructions=f"Do {name}.",
        tools=tuple(_tool(t) for t in tools),
        knowledge=SkillKnowledge(search=search),
    )


def _skills(*available: ResolvedSkill, cap: int = 3) -> SessionSkills:
    base = AgentConfig(
        system_prompt="You are Maya.",
        tools=[_tool("lookup"), _tool("end_call")],
        max_loaded_skills=cap,
    )
    return SessionSkills(base=base, available=available)


class TestLoad:
    def test_a_load_adds_the_section_and_tools_and_is_remembered(self) -> None:
        skills = _skills(_skill("explain_bill", "get_invoices"))

        result = json.loads(skills.load({"name": "explain_bill"}))

        assert result["success"] is True
        assert result["details"] == {"skill": "explain_bill", "version": 1}
        assert "## Skill: explain_bill (v1)\nDo explain_bill." in (
            skills.config().system_prompt
        )
        assert [t.name for t in skills.config().tools] == [
            "load_skill",
            "lookup",
            "end_call",
            "get_invoices",
        ]
        assert [s.name for s in skills.loads] == ["explain_bill"]

    def test_an_unknown_skill_is_answered_and_changes_nothing(self) -> None:
        skills = _skills(_skill("explain_bill"))

        result = json.loads(skills.load({"name": "nope"}))

        assert result["success"] is False
        assert result["message"] == (
            "There is no skill named 'nope' to load. The skills you can load "
            "are: explain_bill."
        )
        assert skills.loaded == () and skills.loads == ()

    def test_a_second_load_of_one_skill_changes_nothing(self) -> None:
        skills = _skills(_skill("explain_bill"))
        skills.load({"name": "explain_bill"})

        result = json.loads(skills.load({"name": "explain_bill"}))

        assert result["details"]["already_loaded"] is True
        assert len(skills.loads) == 1

    def test_past_the_cap_the_oldest_is_unloaded(self) -> None:
        skills = _skills(_skill("a"), _skill("b"), _skill("c"), cap=2)
        for name in ("a", "b"):
            skills.load({"name": name})

        result = json.loads(skills.load({"name": "c"}))

        assert [s.name for s in skills.loaded] == ["b", "c"]
        assert result["details"]["unloaded"] == ["a"]
        assert [s.name for s in skills.loads] == ["a", "b", "c"]


def test_a_stored_list_keeps_what_still_resolves_up_to_the_cap() -> None:
    a, b, c = _skill("a"), _skill("b"), _skill("c")

    held = _held(["a", "gone", "b", "a", 7, "c"], (a, b, c), 2)

    assert held == (b, c)
    assert _held(None, (a,), 3) == ()


def _build(skills: SessionSkills | None, knowledge=None):
    return build_chat_tools(
        skills.base if skills else AgentConfig(),
        session_id=uuid4(),
        project_id=uuid4(),
        knowledge=knowledge,
        openai_api_key="sk-test",
        skills=skills,
    )


@pytest.mark.asyncio
class TestChatTools:
    async def test_load_skill_leads_and_the_skills_tools_follow_a_load(self) -> None:
        skills = _skills(_skill("explain_bill", "get_invoices"))
        tools = await _build(skills)

        assert [s["name"] for s in tools.schemas] == ["load_skill", "lookup"]
        load = tools.schemas[0]["parameters"]["properties"]["name"]
        assert load["enum"] == ["explain_bill"]

        with (
            patch("turncall.services.chat_tools._record_invocation", AsyncMock()),
            patch(
                "turncall.services.chat_tools.post_tool_webhook",
                AsyncMock(return_value='{"ok": true}'),
            ) as webhook,
        ):
            await tools.execute("load_skill", {"name": "explain_bill"})
            answer = await tools.execute("get_invoices", {})

        assert [s["name"] for s in tools.current()] == [
            "load_skill",
            "lookup",
            "get_invoices",
        ]
        assert answer == '{"ok": true}'
        assert webhook.await_args.args[0].name == "get_invoices"

    async def test_an_unloaded_skills_tool_does_not_run(self) -> None:
        tools = await _build(_skills(_skill("explain_bill", "get_invoices")))

        with patch("turncall.services.chat_tools._record_invocation", AsyncMock()):
            answer = await tools.execute("get_invoices", {})

        assert json.loads(answer) == {"error": "Unknown tool: get_invoices"}

    async def test_a_loaded_skills_knowledge_base_is_searched(self) -> None:
        kb = uuid4()
        attachment = {
            "knowledge_base_id": kb,
            "mode": "tool",
            "top_k": 2,
            "similarity_threshold": 0.4,
            "tool_description": "Search the billing FAQ",
        }
        skills = _skills(_skill("explain_bill", search=(attachment,)))
        tools = await _build(skills)
        assert "query_knowledge" not in {s["name"] for s in tools.schemas}

        with (
            patch("turncall.services.chat_tools._record_invocation", AsyncMock()),
            patch(
                "turncall.services.chat_tools._search",
                AsyncMock(return_value="[]"),
            ) as search,
        ):
            await tools.execute("load_skill", {"name": "explain_bill"})
            await tools.execute("query_knowledge", {"query": "late fee"})

        offered = {s["name"]: s for s in tools.current()}
        assert offered["query_knowledge"]["description"] == "Search the billing FAQ"
        [links, args, _key] = search.await_args.args
        assert [link["knowledge_base_id"] for link in links] == [kb]
        assert args == {"query": "late fee"}

    async def test_without_skills_load_skill_is_never_offered(self) -> None:
        config = AgentConfig(tools=[_tool("load_skill")])
        tools = await build_chat_tools(config, session_id=uuid4(), project_id=uuid4())

        assert tools.schemas == []
