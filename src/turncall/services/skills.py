"""Skills on a conversation (#421, #424): what an agent's skills resolve to, and
the prompt and tools a model is given for them.

A call resolves its agent's skills once, when its pipeline is built: each name
to the skill's live published version in the call's project, its instructions
and tool descriptions rendered with the call's `{{variables}}`. Until a skill is
loaded the model sees only its name and description, in a generated prompt
section, plus the built-in `load_skill`; loading one appends its instructions
as a section of their own and adds its tools — and, with its knowledge bases
read at resolution (`skill_knowledge`, #429), its `prompt`-mode documents in
that section.

Tool precedence is built-in, then the agent's own tools, then loaded skills'
tools in load order, then MCP. `skilled_config` puts them in that order and
everything that advertises or registers tools keeps the first of a name, so
both what the model is shown and what runs follow it. A skill tool that takes
the name of an agent tool or a built-in is dropped at resolution.

What an agent may list is checked when it is saved (#428,
`attachment_refusal`): every name must have a live published version, and no
tool of those versions may share a name with a built-in, the agent's own tool
or another listed skill's. The drop at resolution stays for what changes
after the save: a skill version published later, or a stored agent from
before the rule.

No pipecat here: the text channels will build on the same rules (#430).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.models import (
    BUILTIN_TOOL_NAMES,
    AgentConfig,
    SkillRef,
    ToolDefinition,
)
from turncall.services.skill_knowledge import (
    NO_KNOWLEDGE,
    SkillKnowledge,
    resolve_skill_knowledge,
)
from turncall.services.template_renderer import render_template

LOAD_SKILL = "load_skill"

AVAILABLE_HEADING = "## Skills you can load"
AVAILABLE_HINT = (
    "Load a skill with load_skill when the conversation needs it. Its "
    "instructions and tools are added when you load it."
)


@dataclass(frozen=True)
class ResolvedSkill:
    """One skill version as a call runs it: `instructions` and the tools'
    descriptions are already rendered with the call's variables, tools whose
    names the agent already uses are gone, and its knowledge bases are read
    (#429): what a load adds needs no I/O."""

    id: UUID
    name: str
    version: int
    description: str
    instructions: str
    tools: tuple[ToolDefinition, ...]
    knowledge: SkillKnowledge = NO_KNOWLEDGE


async def resolve_skills(
    session: AsyncSession,
    project_id: UUID,
    refs: Sequence[SkillRef],
    *,
    variables: dict[str, str],
    taken_tool_names: Collection[str],
) -> tuple[ResolvedSkill, ...]:
    """Each named skill's live published version, in the agent's order.

    A name with nothing published, or a version that cannot be read, is a
    warning and a skipped skill: a stale skill name must not take calls down.
    A name listed twice is resolved once.
    """
    from turncall.storage.repositories import skill_repo

    resolved: list[ResolvedSkill] = []
    seen: set[str] = set()
    for ref in refs:
        if ref.skill in seen:
            logger.warning("skill_skipped: listed twice", skill=ref.skill)
            continue
        seen.add(ref.skill)
        try:
            row = await skill_repo.get_published(session, project_id, ref.skill)
            if row is None:
                logger.warning(
                    "skill_skipped: no published version of {skill} in the "
                    "project, call runs without it",
                    skill=ref.skill,
                )
                continue
            knowledge = await resolve_skill_knowledge(
                session,
                project_id,
                row.name,
                getattr(row, "knowledge_bases", None) or [],
            )
            resolved.append(_as_resolved(row, variables, taken_tool_names, knowledge))
        except Exception:
            logger.exception(
                "skill_skipped: could not resolve {skill}", skill=ref.skill
            )
    return tuple(resolved)


def _as_resolved(
    row: Any,
    variables: dict[str, str],
    taken_tool_names: Collection[str],
    knowledge: SkillKnowledge = NO_KNOWLEDGE,
) -> ResolvedSkill:
    tools = []
    for raw in row.tools or []:
        tool = ToolDefinition.model_validate(raw)
        if tool.name in taken_tool_names:
            # Refused at save by #428; a skill published before that, or an
            # agent edited after, still must not take over the agent's tool.
            logger.warning(
                "skill_tool_collision_skipped: {skill} tool {tool} collides with "
                "the agent's own tool or a built-in",
                skill=row.name,
                tool=tool.name,
            )
            continue
        tools.append(
            tool.model_copy(
                update={"description": render_template(tool.description, variables)}
            )
        )
    return ResolvedSkill(
        id=row.id,
        name=row.name,
        version=row.version,
        description=row.description,
        instructions=render_template(row.instructions, variables),
        tools=tuple(tools),
        knowledge=knowledge,
    )


@dataclass(frozen=True)
class AttachmentRefusal:
    """Why an agent may not list its skills as written: a 422's message and
    details, naming both sides."""

    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class Attached:
    """One entry of an agent's `skills`, as the version it resolves to now."""

    index: int
    name: str
    tool_names: tuple[str, ...]


async def attachment_refusal(
    session: AsyncSession,
    project_id: UUID,
    skill_names: Sequence[str],
    agent_tool_names: Sequence[str],
) -> AttachmentRefusal | None:
    """Why `skill_names` cannot be an agent's skills, or None if they can.

    Each name must have a live published version in the project — what a
    call runs; a name with only drafts is refused like one never created, as
    a call would skip it. Then the tools of those versions must not collide
    (`tool_collision`). A name listed twice is refused too.
    """
    from turncall.storage.repositories import skill_repo

    attached: list[Attached] = []
    first_index: dict[str, int] = {}
    for index, name in enumerate(skill_names):
        if name in first_index:
            return AttachmentRefusal(
                f"skills[{index}] '{name}' is listed twice "
                f"(first as skills[{first_index[name]}])",
                {"field": f"skills[{index}]", "skill": name},
            )
        first_index[name] = index
        row = await skill_repo.get_published(session, project_id, name)
        if row is None:
            drafts = await skill_repo.count_skills(session, project_id, name=name)
            return _unknown(index, name, has_versions=drafts > 0)
        tools = tuple(str(t.get("name", "")) for t in row.tools or [])
        attached.append(Attached(index, name, tools))
    return tool_collision(agent_tool_names, attached)


def _unknown(index: int, name: str, *, has_versions: bool) -> AttachmentRefusal:
    why = (
        "has no published version in this project; publish one first"
        if has_versions
        else "is not a skill in this project"
    )
    return AttachmentRefusal(
        f"skills[{index}] '{name}' {why}",
        {"field": f"skills[{index}]", "skill": name, "published": False},
    )


def tool_collision(
    agent_tool_names: Sequence[str], attached: Sequence[Attached]
) -> AttachmentRefusal | None:
    """The first skill tool whose name is already taken — by a built-in, the
    agent's own tool, or an earlier listed skill's tool — or None.

    A load would otherwise drop it silently (first of a name wins), so a
    skill's tool would never run on this agent.
    """
    own = set(agent_tool_names)
    claimed: dict[str, Attached] = {}
    for skill in attached:
        side = f"skills[{skill.index}] '{skill.name}' tool"
        for tool in skill.tool_names:
            other = _taken_by(tool, own, claimed)
            if other is not None:
                return AttachmentRefusal(
                    f"{side} '{tool}' collides with {other}",
                    {
                        "field": f"skills[{skill.index}]",
                        "skill": skill.name,
                        "tool": tool,
                        "collides_with": other,
                    },
                )
        for tool in skill.tool_names:
            claimed.setdefault(tool, skill)
    return None


def _taken_by(tool: str, own: set[str], claimed: dict[str, Attached]) -> str | None:
    if tool in BUILTIN_TOOL_NAMES:
        return f"the built-in '{tool}'"
    if tool in own:
        return f"the agent's own tool '{tool}'"
    earlier = claimed.get(tool)
    if earlier is not None:
        return f"skills[{earlier.index}] '{earlier.name}' tool '{tool}'"
    return None


def available_section(skills: Sequence[ResolvedSkill]) -> str:
    """The prompt section listing what the model can load, and how."""
    lines = [f"- {s.name}: {s.description}" for s in skills]
    return "\n".join([AVAILABLE_HEADING, AVAILABLE_HINT, *lines])


def loaded_section(skill: ResolvedSkill) -> str:
    """A loaded skill's instructions, as their own prompt section — followed
    by its `prompt`-mode knowledge bases' documents (#429)."""
    section = f"## Skill: {skill.name} (v{skill.version})\n{skill.instructions}"
    if skill.knowledge.text:
        return f"{section}\n\n{skill.knowledge.text}"
    return section


def load_skill_tool(skills: Sequence[ResolvedSkill]) -> ToolDefinition:
    """The built-in that loads one of `skills`, by name."""
    return ToolDefinition(
        name=LOAD_SKILL,
        description=(
            "Load one of the skills listed under 'Skills you can load', adding "
            "its instructions and tools for the rest of the conversation."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the skill to load",
                    "enum": [s.name for s in skills],
                }
            },
            "required": ["name"],
        },
    )


def skilled_config(
    base: AgentConfig,
    available: Sequence[ResolvedSkill],
    loaded: Sequence[ResolvedSkill] = (),
) -> AgentConfig:
    """`base` as the model runs it with `available` skills, `loaded` of them
    loaded, in load order.

    No skills, no change: the same config, byte for byte. Otherwise the
    prompt gains the list of skills and each loaded skill's section, and the
    tools are `load_skill`, the agent's own, then each loaded skill's — the
    order precedence takes, since every consumer keeps the first of a name.
    """
    if not available:
        return base
    sections = [
        base.system_prompt,
        available_section(available),
        *(loaded_section(s) for s in loaded),
    ]
    tools = [
        load_skill_tool(available),
        *base.tools,
        *(tool for skill in loaded for tool in skill.tools),
    ]
    return base.model_copy(
        update={
            "system_prompt": "\n\n".join(s for s in sections if s),
            "tools": tools,
        }
    )
