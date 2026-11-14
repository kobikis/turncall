"""What a config saved as an agent must name in its own project (#474).

The `api` origin's checks against the database, after the schema and the
rules: an ambience sound, the skills it lists and a procedure's handoff
target are the project's — a typo'd id fails at save rather than silently,
or as a refused handoff, on the call that needed it. Each refusal carries the
422 body the agent endpoints answer (`AgentRefusal.details`). No other origin
runs them: a call resolves each of these against the call's project itself,
a skill that does not resolve being a warning there (#431).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.models import AgentConfig
from turncall.domain.procedures import HANDOFF_TO_AGENT
from turncall.services import skill_library, skills
from turncall.services.agent_rules import AgentRefusal
from turncall.storage.repositories import agent_repo, ambience_repo, skill_repo


def has_project_checks(config: AgentConfig) -> bool:
    """Whether `config` names anything the checks look up."""
    return bool(config.ambience or config.skills or _escalation_targets(config))


async def project_refusal(
    session: AsyncSession, project_id: UUID, config: AgentConfig
) -> AgentRefusal | None:
    """The first of the ambience, skills and escalation checks to refuse, in
    that order (the order the endpoints ran them), or None."""
    for check in (_ambience, _skills, _escalations):
        if (refusal := await check(session, project_id, config)) is not None:
            return refusal
    return None


async def _ambience(
    session: AsyncSession, project_id: UUID, config: AgentConfig
) -> AgentRefusal | None:
    if config.ambience is None:
        return None
    sound = config.ambience.sound
    if await ambience_repo.get_by_id(session, sound, project_id=project_id):
        return None
    return AgentRefusal(
        f"ambience.sound {sound} is not an ambience sound in this project",
        "ambience.sound",
        details={"field": "ambience.sound", "sound": str(sound)},
    )


async def _skills(
    session: AsyncSession, project_id: UUID, config: AgentConfig
) -> AgentRefusal | None:
    """The skills an agent lists must be live where the project's skills come
    from — itself, or its skill library (#432) — or, pinned, name a
    published or archived version of that skill there (#426) — and bring no
    tool whose name is taken (#428): checked against the version each
    resolves to now, at save, rather than skipped or dropped on a call."""
    if not config.skills:
        return None
    source = await skill_library.skill_source(session, project_id)
    # The lock a skill delete takes, in the project holding the skill: a
    # delete racing this save either lands first and is seen below, or waits
    # and then sees this agent.
    if source.project_id is not None:
        for name in sorted({ref.skill for ref in config.skills}):
            await skill_repo.lock_name(session, source.project_id, name)
    refusal = await skills.attachment_refusal(
        session, source, config.skills, [tool.name for tool in config.tools]
    )
    if refusal is None:
        return None
    return AgentRefusal(
        refusal.message, refusal.details.get("field"), details=refusal.details
    )


def _escalation_targets(config: AgentConfig) -> list[tuple[str, str, str]]:
    """Each procedure handoff escalation as (agent id, message, field)."""
    targets = []
    for index, procedure in enumerate(config.procedures):
        for at, step in enumerate(procedure.steps):
            action = step.escalate.action() if step.escalate else None
            if action is None or action[0] != HANDOFF_TO_AGENT:
                continue
            agent_id = action[1]["agent_id"]
            message = (
                f"procedures[{index}] '{procedure.name}': step '{step.id}' "
                f"hands off to '{agent_id}', which is not an agent in this project"
            )
            field = (
                f"procedures[{index}].steps[{at}].escalate.{HANDOFF_TO_AGENT}.agent_id"
            )
            targets.append((agent_id, message, field))
    return targets


async def _escalations(
    session: AsyncSession, project_id: UUID, config: AgentConfig
) -> AgentRefusal | None:
    """A procedure's handoff escalation names an agent of this project
    (#448)."""
    for agent_id, message, field in _escalation_targets(config):
        agent = await agent_repo.get_agent_by_id(
            session, UUID(agent_id), project_id=project_id
        )
        if agent is None:
            return AgentRefusal(
                message, field, details={"field": field, "agent_id": agent_id}
            )
    return None
