"""Skills on a text conversation (#430): the Chat API, SMS and WhatsApp text.

A text session holds skills the way a call does (#424), across messages
rather than inside one pipeline:

- **Resolved once per session.** The first message of a session whose agent
  lists skills resolves them (`skills.resolve_skills`: pins honoured, the live
  published version otherwise, where the session's project reads its
  skills: itself, or its skill library, #433) and stores the
  versions in the session's metadata under `resolved_skills`, the shape a
  call's row keeps (`skills.resolution_entries`). Every later message runs
  those same versions, re-read by id, so a publish mid-session does not change
  a live conversation — as a publish mid-call does not change the call. What
  changes per message is the rendering: instructions and tool descriptions
  take the session's `{{variables}}` as they stand, like the agent's own
  prompt does.
- **Loaded across messages.** `load_skill` in a turn's tool loop loads the
  skill for the rest of that loop (`llm_text`'s `refresh`: the next round is
  given the skill's section and tools) and, once the reply is stored, for the
  session: the names go under `loaded_skills`, in load order. A new session
  starts with none.
- **The voice rules, not a copy of them.** The cap and which skill a load
  unloads (`skills.after_load`), the already-loaded no-op and the answers the
  model reads are `services/skills`'s, shared with `orchestrator/skill_loader`.

Two messages of one session may run at once. Both writes lock the session's
row and merge one key (`sms_session_repo.lock_session_metadata` /
`merge_session_metadata`): the resolution is written by whichever comes first
and read by the other, and a turn's loads are replayed onto the stored list as
it is when the turn ends, so neither message's load is lost (the cap still
applies to the result).

What records a text load: the `load_skill` row in `tool_invocations`, like any
text tool, and the session's `loaded_skills`. A text session has no call event
log, so there is no `skill.loaded` event.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.models import BUILTIN_TOOL_NAMES, AgentConfig, SkillRef
from turncall.services.skill_knowledge import searched_attachments
from turncall.services.skills import (
    ResolvedSkill,
    after_load,
    already_loaded_result,
    loaded_result,
    resolution_entries,
    resolve_skills,
    skilled_config,
    unknown_skill_result,
)
from turncall.storage.repositories import sms_session_repo

# The key a call's row keeps its resolution under (`call_view`), on purpose.
RESOLVED_SKILLS = "resolved_skills"
LOADED_SKILLS = "loaded_skills"


@dataclass
class SessionSkills:
    """One text turn's skills: the agent's config before any of them, what
    the session resolved, what is loaded, and what this turn loaded.

    Not frozen: a load in one round of the turn's tool loop must reach the
    next. `loaded` and `loads` are replaced whole, only by `load`.
    """

    base: AgentConfig
    available: tuple[ResolvedSkill, ...]
    loaded: tuple[ResolvedSkill, ...] = ()
    # Loaded by this turn, in order: what `persist_loads` replays.
    loads: tuple[ResolvedSkill, ...] = ()

    def find(self, name: str) -> ResolvedSkill | None:
        return next((s for s in self.available if s.name == name), None)

    @property
    def cap(self) -> int:
        return self.base.max_loaded_skills

    def config(self) -> AgentConfig:
        """The config the model runs on now: `skilled_config`, as a call's."""
        return skilled_config(self.base, self.available, self.loaded)

    def searches(self) -> bool:
        """Whether a loaded skill has a tool-mode knowledge base."""
        return any(s.knowledge.search for s in self.loaded)

    def searched(self, own: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """What `query_knowledge` searches: `own` (the agent's tool-mode
        attachments), then the loaded skills', each KB once."""
        return searched_attachments(own, self.loaded)

    def load(self, args: dict[str, Any]) -> str:
        """The `load_skill` tool on text. No I/O: the session's record is
        written once the turn's reply is (`persist_loads`)."""
        name = str(args.get("name") or "")
        skill = self.find(name)
        if skill is None:
            return unknown_skill_result(name, self.available)
        if any(s.name == name for s in self.loaded):
            return already_loaded_result(skill)
        self.loaded, unloaded = after_load(self.loaded, skill, self.cap)
        self.loads = (*self.loads, skill)
        logger.info(
            "load_skill: loaded {skill} v{version} on text, unloaded {unloaded}",
            skill=name,
            version=skill.version,
            unloaded=[s.name for s in unloaded],
        )
        return loaded_result(skill, unloaded, self.cap)


async def session_skills(
    db: AsyncSession,
    *,
    project_id: UUID,
    session_id: UUID,
    config: AgentConfig,
    variables: dict[str, str],
) -> SessionSkills | None:
    """The session's skills for this turn, or None for an agent with none.

    Resolves and stores them on the session's first message with skills, else
    re-reads the stored versions; commits. Never fails the message: anything
    that goes wrong is logged and the turn runs without skills.
    """
    if not config.skills:
        return None
    taken = {t.name for t in config.tools} | BUILTIN_TOOL_NAMES
    try:
        async with db.begin_nested():
            metadata = await sms_session_repo.lock_session_metadata(db, session_id)
            stored = metadata.get(RESOLVED_SKILLS)
            if isinstance(stored, list):
                available = await _stored_versions(
                    db, project_id, config, stored, variables, taken
                )
            else:
                available = await resolve_skills(
                    db,
                    project_id,
                    config.skills,
                    variables=variables,
                    taken_tool_names=taken,
                )
                await sms_session_repo.merge_session_metadata(
                    db, session_id, {RESOLVED_SKILLS: resolution_entries(available)}
                )
        await db.commit()
    except Exception:
        logger.exception(
            "skills_skipped: could not resolve the session's skills",
            session_id=str(session_id),
        )
        return None
    if not available:
        return None
    skills = SessionSkills(base=config, available=available)
    skills.loaded = _held(metadata.get(LOADED_SKILLS), available, skills.cap)
    return skills


async def _stored_versions(
    db: AsyncSession,
    project_id: UUID,
    config: AgentConfig,
    stored: list[Any],
    variables: dict[str, str],
    taken: set[str],
) -> tuple[ResolvedSkill, ...]:
    """The versions the session resolved, for the skills the agent still
    lists, in the agent's order — each read by id, as a pin is. A name the
    agent lists that the session never resolved stays unresolved."""
    entries = {
        e["skill"]: e
        for e in stored
        if isinstance(e, dict) and isinstance(e.get("skill"), str) and e.get("id")
    }
    refs = [
        SkillRef(skill=ref.skill, version_id=UUID(str(entries[ref.skill]["id"])))
        for ref in config.skills
        if ref.skill in entries
    ]
    resolved = await resolve_skills(
        db, project_id, refs, variables=variables, taken_tool_names=taken
    )
    return tuple(
        replace(s, pinned=entries[s.name].get("pinned") is True) for s in resolved
    )


def _held(
    names: Any, available: tuple[ResolvedSkill, ...], cap: int
) -> tuple[ResolvedSkill, ...]:
    """The stored `loaded_skills` as skills, in load order: a name no longer
    resolved is dropped, and at most `cap` kept, the latest."""
    if not isinstance(names, list):
        return ()
    by_name = {s.name: s for s in available}
    held: list[ResolvedSkill] = []
    for name in names:
        skill = by_name.get(name) if isinstance(name, str) else None
        if skill is not None and skill not in held:
            held.append(skill)
    return tuple(held[-cap:])


async def persist_loads(
    db: AsyncSession, session_id: UUID, skills: SessionSkills | None
) -> None:
    """Store what the turn loaded on the session: its loads replayed, in
    order and under the cap, onto `loaded_skills` as it is now — a message of
    the same session that ran meanwhile kept. The caller commits, with the
    turn's reply."""
    if skills is None or not skills.loads:
        return
    metadata = await sms_session_repo.lock_session_metadata(db, session_id)
    held = _held(metadata.get(LOADED_SKILLS), skills.available, skills.cap)
    for skill in skills.loads:
        held, _ = after_load(held, skill, skills.cap)
    await sms_session_repo.merge_session_metadata(
        db, session_id, {LOADED_SKILLS: [s.name for s in held]}
    )
