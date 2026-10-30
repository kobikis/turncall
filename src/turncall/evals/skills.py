"""An eval run's skills (#431): resolved once, before the first iteration.

A call resolves its agent's skills at build. An eval builds a pipeline per
iteration, so left alone each iteration resolved them again — and a skill
published while the run was going would have been run by some iterations and
not others, with the run's snapshot naming neither. Instead the run resolves
them once, as it resolves its target: the versions go into `resolved_config`
(`resolved_skills`, a call row's shape — `[{id, skill, version, pinned}]`),
and every iteration runs those versions, pinned. A skill that does not resolve
is left out of the run and said so in the run's warnings, as on a call it is a
warning and a skipped skill.

Resolved in the run's project — or, for a project linked to a skill library,
in the library (#433, ADR-0031: `resolve_skills` reads the project's source) —
for every target form an inline one included, with no `{{variables}}`: an
eval has no call row to hold any.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

from turncall.domain.models import BUILTIN_TOOL_NAMES, SkillRef
from turncall.evals.targets import ResolvedTarget
from turncall.orchestrator.config_warnings import skills_on_s2s_warning
from turncall.services.skills import (
    ResolvedSkill,
    resolution_entries,
    resolve_skills,
)

# The key in `resolved_config` the resolution is written under — the same
# name a call row's metadata uses for it.
RESOLVED_SKILLS = "resolved_skills"


@dataclass(frozen=True)
class RunSkills:
    """What a run resolved: the target its iterations run (skills pinned to
    the resolved versions), what the snapshot records, and what the run must
    tell its author."""

    target: ResolvedTarget
    available: tuple[ResolvedSkill, ...] = ()
    warnings: list[dict[str, Any]] = field(default_factory=list)
    # Whether the agent lists any skills at all: only then does the snapshot
    # carry a resolution (an empty one when none resolved).
    listed: bool = False

    def snapshot(self, resolved_config: dict[str, Any]) -> dict[str, Any]:
        """`resolved_config` with the resolution, for an agent listing skills;
        unchanged for one that lists none."""
        if not self.listed:
            return resolved_config
        return {**resolved_config, RESOLVED_SKILLS: resolution_entries(self.available)}

    @property
    def tool_names(self) -> set[str]:
        """Every tool a loaded skill can bring: a mock may name any of them."""
        return {t.name for s in self.available for t in s.tools}


async def resolve_run_skills(session: Any, target: ResolvedTarget) -> RunSkills:
    """Resolve the target's skills once for the whole run. Never raises: a
    run whose skills cannot be read runs without them, with a warning."""
    config = target.config
    if not config.skills:
        return RunSkills(target=target)
    on_s2s = skills_on_s2s_warning(config)
    if on_s2s is not None:
        return RunSkills(target=_without_skills(target), warnings=[on_s2s], listed=True)
    try:
        available = await resolve_skills(
            session,
            target.project_id,
            config.skills,
            variables={},
            taken_tool_names={t.name for t in config.tools} | BUILTIN_TOOL_NAMES,
        )
    except Exception:
        logger.exception("eval_skills_unresolved", project_id=str(target.project_id))
        available = ()
    pinned = config.model_copy(
        update={"skills": [SkillRef(skill=s.name, version_id=s.id) for s in available]}
    )
    resolved = {s.name for s in available}
    missing = list(
        dict.fromkeys(r.skill for r in config.skills if r.skill not in resolved)
    )
    return RunSkills(
        target=replace(target, config=pinned),
        available=available,
        warnings=[_skills_skipped(missing)] if missing else [],
        listed=True,
    )


def _without_skills(target: ResolvedTarget) -> ResolvedTarget:
    return replace(target, config=target.config.model_copy(update={"skills": []}))


def _skills_skipped(names: list[str]) -> dict[str, Any]:
    return {
        "code": "skills_skipped",
        "message": (
            f"{len(names)} skill(s) did not resolve and the run "
            "went without them: "
            + ", ".join(names)
            + ". A skill needs a published version (or a pin on a published or "
            "archived one) in the run's project, or in its skill library when "
            "it is linked to one."
        ),
        "skills": names,
    }
