"""Skills on a conversation (#421, #424): what an agent's skills resolve to, and
the prompt and tools a model is given for them.

A call resolves its agent's skills once, when its pipeline is built: each name
to the skill's live published version in the project its skills come from —
the call's own, or the skill library it is linked to (#432, ADR-0031,
`skill_library.skill_source`) — or, for an entry
that pins one (#426, ADR-0030), to exactly that version, published or archived
— its instructions and tool descriptions rendered with the call's
`{{variables}}`. Until a skill is
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
`attachment_refusal`): every name must have a live published version in the
project's skill source (a pin: must name a published or archived version of
that skill there), and
no tool of those versions may share a name with a built-in, the agent's own
tool or another listed skill's. The drop at resolution stays for what changes
after the save: a skill version published later, or a stored agent from
before the rule.

What a `load_skill` holds and answers is decided here too, for voice and
text alike (#430): `after_load` (the `max_loaded_skills` cap, the skill
loaded longest ago unloaded first) and the result the model reads for an
unknown skill, one already loaded, and a load.

No pipecat here: the text channels build on the same rules (#430).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import SkillState
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
from turncall.services.skill_library import SkillSource, skill_source
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
    # The agent named this version (#426) rather than following the live one.
    pinned: bool = False


async def resolve_skills(
    session: AsyncSession,
    project_id: UUID,
    refs: Sequence[SkillRef],
    *,
    variables: dict[str, str],
    taken_tool_names: Collection[str],
) -> tuple[ResolvedSkill, ...]:
    """Each named skill's version, in the agent's order: the pinned one for an
    entry with `version_id` (#426), else the live published one — looked up
    where `project_id`'s skills come from: its skill library when it is
    linked to one, else itself (#432).

    A name with nothing published, a pin that names no version of the skill
    there (or a draft), a library that is gone, or a version that cannot be
    read, is a warning and a skipped skill: a stale skill name must not take
    calls down. A name listed twice is resolved once.
    """
    if not refs:
        return ()
    source = await skill_source(session, project_id)
    if source.project_id is None:
        logger.warning(
            "skills_skipped: the project's skill library is gone, the "
            "conversation runs without {skills}",
            skills=[ref.skill for ref in refs],
        )
        return ()
    resolved: list[ResolvedSkill] = []
    seen: set[str] = set()
    for ref in refs:
        if ref.skill in seen:
            logger.warning("skill_skipped: listed twice", skill=ref.skill)
            continue
        seen.add(ref.skill)
        try:
            row = await _version_for(session, source, ref)
            if row is None:
                continue
            # Checked against the project that holds the skill (#433): a
            # library skill's KBs are the library's.
            knowledge = await resolve_skill_knowledge(
                session,
                source.project_id,
                row.name,
                getattr(row, "knowledge_bases", None) or [],
            )
            resolved.append(
                _as_resolved(
                    row,
                    variables,
                    taken_tool_names,
                    knowledge,
                    pinned=ref.version_id is not None,
                )
            )
        except Exception:
            logger.exception(
                "skill_skipped: could not resolve {skill}", skill=ref.skill
            )
    return tuple(resolved)


async def _version_for(
    session: AsyncSession, source: SkillSource, ref: SkillRef
) -> Any | None:
    """The skill version `ref` runs, or None (logged) when there is none."""
    from turncall.storage.repositories import skill_repo

    assert source.project_id is not None  # a gone library resolves nothing
    if ref.version_id is None:
        row = await skill_repo.get_published(session, source.project_id, ref.skill)
        if row is None:
            logger.warning(
                "skill_skipped: no published version of {skill} in {where}, "
                "call runs without it",
                skill=ref.skill,
                where=source.where,
            )
        return row
    row = await skill_repo.get_by_id(
        session, ref.version_id, project_id=source.project_id
    )
    why = pin_refusal(ref.skill, row, where=source.where)
    if why is not None:
        logger.warning(
            "skill_skipped: pinned version {version_id} {why}, call runs "
            "without {skill}",
            skill=ref.skill,
            version_id=str(ref.version_id),
            why=why,
        )
        return None
    return row


def pin_refusal(
    name: str, row: Any | None, *, where: str = "this project"
) -> str | None:
    """Why a pin on skill `name` cannot run the version `row` (looked up by
    id where the agent's skills come from, `where`), or None if it can. A pin
    runs a published or an archived version — the pin is what keeps an agent
    on a version after the next publish archives it — never a draft, which
    can still change."""
    if row is None:
        return f"is not a version of a skill in {where}"
    if row.name != name:
        return f"is a version of '{row.name}', not of '{name}'"
    if row.state == SkillState.DRAFT.value:
        return f"is a draft (v{row.version}); pin a published or archived version"
    return None


def _as_resolved(
    row: Any,
    variables: dict[str, str],
    taken_tool_names: Collection[str],
    knowledge: SkillKnowledge = NO_KNOWLEDGE,
    *,
    pinned: bool = False,
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
        pinned=pinned,
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


class Ref(Protocol):
    """An entry of an agent's `skills`: the domain `SkillRef` or the API's."""

    skill: str
    version_id: UUID | None


async def attachment_refusal(
    session: AsyncSession,
    source: SkillSource,
    refs: Sequence[Ref],
    agent_tool_names: Sequence[str],
) -> AttachmentRefusal | None:
    """Why `refs` cannot be an agent's skills, or None if they can.

    Checked in `source`, where the agent's project resolves skills: itself,
    or its skill library (#432). Each entry must resolve to what a call would
    run: its pinned version (#426) — a published or archived version of that
    skill there — or, unpinned, a live published version; a name with only
    drafts is refused like one never created, as a call would skip it. Then
    the tools of those versions must not collide (`tool_collision`). A name
    listed twice is refused too, pinned or not: a call holds one version of a
    skill. A library that is gone holds no skill.
    """
    from turncall.storage.repositories import skill_repo

    attached: list[Attached] = []
    first_index: dict[str, int] = {}
    for index, ref in enumerate(refs):
        name = ref.skill
        if name in first_index:
            return AttachmentRefusal(
                f"skills[{index}] '{name}' is listed twice "
                f"(first as skills[{first_index[name]}])",
                {"field": f"skills[{index}]", "skill": name},
            )
        first_index[name] = index
        if source.project_id is None:
            return _library_gone(index, name)
        if ref.version_id is not None:
            row = await skill_repo.get_by_id(
                session, ref.version_id, project_id=source.project_id
            )
            why = pin_refusal(name, row, where=source.where)
            if row is None or why is not None:
                return _bad_pin(index, name, ref.version_id, why or "")
        else:
            row = await skill_repo.get_published(session, source.project_id, name)
            if row is None:
                drafts = await skill_repo.count_skills(
                    session, source.project_id, name=name
                )
                return _unknown(index, name, source, has_versions=drafts > 0)
        tools = tuple(str(t.get("name", "")) for t in row.tools or [])
        attached.append(Attached(index, name, tools))
    return tool_collision(agent_tool_names, attached)


def _bad_pin(index: int, name: str, version_id: UUID, why: str) -> AttachmentRefusal:
    return AttachmentRefusal(
        f"skills[{index}] '{name}' version_id '{version_id}' {why}",
        {
            "field": f"skills[{index}].version_id",
            "skill": name,
            "version_id": str(version_id),
        },
    )


def _unknown(
    index: int, name: str, source: SkillSource, *, has_versions: bool
) -> AttachmentRefusal:
    why = (
        f"has no published version in {source.where}; publish one first"
        if has_versions
        else f"is not a skill in {source.where}"
    )
    return AttachmentRefusal(
        f"skills[{index}] '{name}' {why}",
        {"field": f"skills[{index}]", "skill": name, "published": False},
    )


def _library_gone(index: int, name: str) -> AttachmentRefusal:
    return AttachmentRefusal(
        f"skills[{index}] '{name}' cannot be resolved: this project's skill "
        "library is gone",
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


def resolution_entries(available: Sequence[ResolvedSkill]) -> list[dict[str, Any]]:
    """What a conversation resolved, as it is stored on its record (#425,
    #426): `[{id, skill, version, pinned}]` — a call's row, a text session's
    metadata (#430)."""
    return [
        {"id": str(s.id), "skill": s.name, "version": s.version, "pinned": s.pinned}
        for s in available
    ]


def after_load(
    loaded: Sequence[ResolvedSkill], skill: ResolvedSkill, cap: int
) -> tuple[tuple[ResolvedSkill, ...], tuple[ResolvedSkill, ...]]:
    """What is loaded once `skill` is, and what that unloads (#427): `skill`
    joins the end of `loaded` (or stays where it is, already there), and past
    `cap` the skills loaded longest ago go, oldest first."""
    if any(s.name == skill.name for s in loaded):
        held = tuple(loaded)
    else:
        held = (*loaded, skill)
    over = max(len(held) - cap, 0)
    return held[over:], held[:over]


def load_result(success: bool, message: str, details: dict[str, Any] | None) -> str:
    """`load_skill`'s result, as the model reads it."""
    return json.dumps({"success": success, "message": message, "details": details})


def unknown_skill_result(name: str, available: Sequence[ResolvedSkill]) -> str:
    """A `load_skill` naming no skill the conversation can load."""
    names = ", ".join(s.name for s in available)
    return load_result(
        False,
        f"There is no skill named {name!r} to load."
        + (f" The skills you can load are: {names}." if names else ""),
        None,
    )


def already_loaded_result(skill: ResolvedSkill) -> str:
    """A `load_skill` of a skill already loaded: nothing changes."""
    return load_result(
        True,
        f"The skill {skill.name!r} is already loaded: its instructions and tools "
        "are in place. Continue the conversation.",
        {"skill": skill.name, "version": skill.version, "already_loaded": True},
    )


def loaded_result(
    skill: ResolvedSkill, unloaded: Sequence[ResolvedSkill], cap: int
) -> str:
    """A `load_skill` that loaded `skill`, and unloaded `unloaded` to stay
    within `cap`."""
    note = ""
    if unloaded:
        names = ", ".join(repr(s.name) for s in unloaded)
        note = (
            f" At most {cap} skills can be loaded at once, so {names} was "
            "unloaded: load it again if you need it."
        )
    return load_result(
        True,
        f"The skill {skill.name!r} is loaded: follow its instructions, and its "
        "tools are now available." + note + " Continue the conversation.",
        {
            "skill": skill.name,
            "version": skill.version,
            **({"unloaded": [s.name for s in unloaded]} if unloaded else {}),
        },
    )
