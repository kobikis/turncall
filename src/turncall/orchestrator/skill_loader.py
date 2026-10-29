"""Loading a skill on a voice call (#424).

At build, `resolve_call_skills` resolves the agent's skills once, against the
call's project, and the call runs `CallSkills.config()`: the agent's config
with the list of skills in its prompt and `load_skill` among its tools. The
model's `load_skill` then runs `run_load_skill`, which follows the handoff's
prepare-then-apply discipline (#144) over the same switch (#422): the new
config and both frames are built first, where raising is still harmless, and
applied with nothing left to wait on — but, unlike a handoff, over the context
as it is, so the conversation so far is kept.

`CallSkills.available` is what the call resolved, versions included;
`loaded` is what it has loaded, in load order, at most the agent's
`max_loaded_skills` (#427): a load past the cap unloads the skill loaded
longest ago in the same switch, and loading a skill already loaded changes
nothing. Both reach the call record (#425): the resolution is saved on the
call row at build (`record_resolution`), and each load is a `skill.loaded`
event (each unload a `skill.unloaded`), so the call read and `call.ended` can
say which version of which skill the call ran after the skill has been
published again.

A handoff (#427) gives the call the target agent's skills, none loaded:
`prepare_handoff_skills` resolves them before the handoff commits, and the
handoff's apply step swaps them in (`CallSkills.adopt`) with nothing left to
wait on. Every `CallContext` starts with an empty `CallSkills`, filled at
build for an agent with skills, so there is always one to swap into.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from loguru import logger

from turncall.domain.enums import CallEventType
from turncall.domain.models import BUILTIN_TOOL_NAMES
from turncall.services.skills import ResolvedSkill, resolve_skills, skilled_config

if TYPE_CHECKING:
    from turncall.domain.models import AgentConfig
    from turncall.orchestrator.pipeline_factory import CallContext
    from turncall.orchestrator.tool_bridge import SwitchInputs


@dataclass
class CallSkills:
    """One call's skills: the agent's config before any of them, the skills it
    resolved, the inputs its prompt and tools were built from, and the ones
    loaded so far.

    Not frozen, unlike the rest of the call's context: which skills are loaded
    is the one thing about it that changes mid-call — and, at a handoff, which
    agent they are for. `loaded` is replaced whole, never appended to, only
    by `apply_load`; the rest only by `adopt`.
    """

    base: AgentConfig
    available: tuple[ResolvedSkill, ...]
    switch: SwitchInputs
    loaded: tuple[ResolvedSkill, ...] = ()

    def find(self, name: str) -> ResolvedSkill | None:
        return next((s for s in self.available if s.name == name), None)

    def is_loaded(self, name: str) -> bool:
        return any(s.name == name for s in self.loaded)

    @property
    def cap(self) -> int:
        """How many may be loaded at once: the agent's `max_loaded_skills`."""
        return self.base.max_loaded_skills

    def adopt(self, other: CallSkills) -> None:
        """Become `other`: a handoff's target agent's skills. Plain
        assignments, so it cannot suspend or raise — it runs inside the
        handoff's apply step (`test_handoff_switch_is_atomic.py`)."""
        self.base = other.base
        self.available = other.available
        self.switch = other.switch
        self.loaded = other.loaded

    def config(self, loaded: tuple[ResolvedSkill, ...] | None = None) -> AgentConfig:
        """The config the model runs on with `loaded` (default: what is)."""
        return skilled_config(
            self.base, self.available, self.loaded if loaded is None else loaded
        )


def no_skills(config: AgentConfig) -> CallSkills:
    """A holder with no skills — what every `CallContext` starts with, and
    what a call whose agent resolved none keeps: nothing to load until a
    handoff brings some (#427)."""
    from turncall.orchestrator.tool_bridge import SwitchInputs

    return CallSkills(base=config, available=(), switch=SwitchInputs())


async def prepare_handoff_skills(
    config: AgentConfig, call_context: CallContext
) -> tuple[CallSkills, AgentConfig]:
    """The target agent's skills, resolved as at a call's start (its skills'
    live published versions, the call's project and `{{variables}}`), none
    loaded — and the config the call then runs: the target's own with the
    list and `load_skill`, or unchanged for a target with none.

    I/O, so it runs in the handoff's prepare step, before the switch commits.
    A handoff's switch keeps none of the call's preamble or discovered tools
    (`SwitchInputs()`), so neither does a later load on the target.
    """
    from turncall.orchestrator.tool_bridge import SwitchInputs

    available = await resolve_call_skills(config, call_context)
    skills = CallSkills(base=config, available=available, switch=SwitchInputs())
    return skills, skills.config()


async def resolve_call_skills(
    config: AgentConfig, call_context: CallContext
) -> tuple[ResolvedSkill, ...]:
    """The agent's skills as this call runs them, or none.

    Cascade voice only: a realtime session reads no system or tool change
    made after it starts on most providers (#428 warns about it). A call with
    no database behind it — the website demo — has no project to resolve a
    skill in. Nothing here can fail the call: a skill that does not resolve is
    a warning and a skipped skill.
    """
    if not config.skills:
        return ()
    if config.pipeline_mode == "s2s":
        logger.info("skills_skipped: an S2S agent loads no skills")
        return ()
    if not call_context.has_database:
        logger.warning("skills_skipped: no database to resolve skills against")
        return ()
    try:
        async with call_context.session_factory() as session:  # type: ignore[misc]
            variables = await _call_variables(session, call_context)
            return await resolve_skills(
                session,
                call_context.project_id,
                config.skills,
                variables=variables,
                taken_tool_names={t.name for t in config.tools} | BUILTIN_TOOL_NAMES,
            )
    except Exception:
        logger.exception("skills_skipped: could not resolve the agent's skills")
        return ()


async def _call_variables(session: Any, call_context: CallContext) -> dict[str, str]:
    """The call's `{{variables}}`, as every voice entry point stored them on
    the call row (the agent's own config was rendered with the same). An eval
    has no row, so none."""
    if not call_context.records:
        return {}
    from turncall.storage.repositories import call_repo

    call = await call_repo.get_call_by_id(session, call_context.call_id)
    variables = ((call.metadata_json or {}) if call else {}).get("template_variables")
    return variables if isinstance(variables, dict) else {}


async def record_resolution(
    call_context: CallContext, available: tuple[ResolvedSkill, ...]
) -> None:
    """Save the skills the call resolved on its row (#425):
    `[{id, skill, version}]` under `metadata_json["resolved_skills"]` — at
    build, and again after a handoff for the target agent's (#427). Appended,
    a version already there skipped, so the read lists every skill version
    the call could have run, the first agent's first.

    Only for a call with a row (`records`). Best-effort: a call whose record
    could not be written still runs its skills, and its read shows none.
    """
    if not available or not call_context.records:
        return
    from turncall.services.call_view import RESOLVED_SKILLS
    from turncall.storage.repositories import call_repo

    resolution = [
        {"id": str(s.id), "skill": s.name, "version": s.version} for s in available
    ]
    try:
        async with call_context.session_factory() as session:  # type: ignore[misc]
            await call_repo.append_call_metadata_list(
                session,
                call_context.call_id,
                RESOLVED_SKILLS,
                resolution,
                unique_by="id",
            )
            await session.commit()
    except Exception:
        logger.exception(
            "skills_resolution_not_recorded", call_id=str(call_context.call_id)
        )


# Why a skill was loaded: the model asked (`load_skill`). Why one was
# unloaded: a load past `max_loaded_skills` made room (#427).
LOADED_BY_MODEL = "load_skill"
UNLOADED_FOR_LIMIT = "limit"


class SkillEvent(NamedTuple):
    event_type: CallEventType
    skill: ResolvedSkill
    reason: str


def record_skill_event(
    call_context: CallContext,
    event_type: CallEventType,
    skill: ResolvedSkill,
    *,
    reason: str,
) -> None:
    """A `skill.loaded` / `skill.unloaded` event `{skill, version, reason}`;
    see `record_skill_events`."""
    record_skill_events(call_context, [SkillEvent(event_type, skill, reason)])


def record_skill_events(call_context: CallContext, events: list[SkillEvent]) -> None:
    """`skill.loaded` / `skill.unloaded` events `{skill, version, reason}`,
    stored on the call's log and sent to subscribers, off the critical path
    (as a tool's `tool.result` is): the write and a webhook's retries must not
    sit between the load and the model's next inference. One task writes them
    in order, so an unload is logged before the load that made it. Nothing for
    a call with no row (an eval)."""
    if not events or not call_context.records:
        return
    from turncall.orchestrator.tool_bridge import _spawn

    _spawn(_emit_skill_events(call_context, events))


async def _emit_skill_events(
    call_context: CallContext, events: list[SkillEvent]
) -> None:
    from turncall.events.emit import emit_call_event

    for event in events:
        payload = {
            "skill": event.skill.name,
            "version": event.skill.version,
            "reason": event.reason,
        }
        try:
            # Identifiers only (redaction's structural fields): a plain
            # payload, no detector to wait on.
            async with call_context.session_factory() as session:  # type: ignore[misc]
                await emit_call_event(
                    session,
                    call_id=call_context.call_id,
                    project_id=call_context.project_id,
                    event_type=event.event_type,
                    payload=payload,
                    commit=True,
                )
        except Exception:
            logger.exception(
                "skill_event_not_recorded",
                call_id=str(call_context.call_id),
                event_type=str(event.event_type),
            )


class PreparedLoad(NamedTuple):
    """A load with everything built and nothing applied. `unloaded` are the
    skills it takes off to stay within the cap, longest loaded first."""

    skill: ResolvedSkill
    loaded: tuple[ResolvedSkill, ...]
    config: AgentConfig
    frames: tuple[Any, ...]
    unloaded: tuple[ResolvedSkill, ...] = ()


def prepare_load(skills: CallSkills, skill: ResolvedSkill) -> PreparedLoad:
    """The config and both frames for `skill` loaded — and, past the cap, the
    skills loaded longest ago unloaded, in the same config and frames (#427),
    so the unload and the load are one switch. Pure, and may raise: nothing
    has been applied yet. A skill already loaded is answered before this
    (`run_load_skill`); here it would stay where it is."""
    from turncall.orchestrator.tool_bridge import build_switch_frames

    if skills.is_loaded(skill.name):
        held = skills.loaded
    else:
        held = (*skills.loaded, skill)
    over = max(len(held) - skills.cap, 0)
    unloaded, loaded = held[:over], held[over:]
    config = skills.config(loaded)
    return PreparedLoad(
        skill, loaded, config, build_switch_frames(config, skills.switch), unloaded
    )


async def apply_load(
    prepared: PreparedLoad,
    skills: CallSkills,
    call_context: CallContext,
    params: Any,
) -> None:
    """Apply a prepared load. Atomically: `apply_switch` cannot suspend, and
    neither may anything added here — `test_handoff_switch_is_atomic.py`
    fails if it does."""
    from turncall.orchestrator.tool_bridge import apply_switch

    await apply_switch(
        prepared.config, prepared.frames, call_context, params, clear_context=False
    )
    skills.loaded = prepared.loaded


def _result(success: bool, message: str, details: dict[str, Any] | None) -> str:
    return json.dumps({"success": success, "message": message, "details": details})


async def run_load_skill(
    args: dict[str, Any], call_context: CallContext, params: Any
) -> str:
    """The `load_skill` built-in: prepare, apply, then deliver the result.

    The result asks for no inference of its own. The switch's frames are
    queued at the pipeline's head and reach the user aggregator and the LLM in
    order, so an inference the result triggered from the assistant aggregator
    could run before them, on the old prompt and tools. Instead, once the
    result is in the context, an `LLMRunFrame` is queued behind the switch:
    the model's next inference sees the skill, and the conversation with it.
    """
    skills: CallSkills | None = getattr(call_context, "skills", None)
    name = str(args.get("name") or "")
    skill = skills.find(name) if skills is not None else None
    if skills is None or skill is None:
        names = ", ".join(s.name for s in skills.available) if skills else ""
        result = _result(
            False,
            f"There is no skill named {name!r} to load."
            + (f" The skills you can load are: {names}." if names else ""),
            None,
        )
        await params.result_callback(result)
        return result

    if skills.is_loaded(name):
        # Nothing to switch: no frames, no event, the prompt and tools as
        # they are. The model's next inference runs as after any result.
        result = _result(
            True,
            f"The skill {name!r} is already loaded: its instructions and tools "
            "are in place. Continue the conversation.",
            {"skill": skill.name, "version": skill.version, "already_loaded": True},
        )
        await params.result_callback(result)
        return result

    try:
        prepared = prepare_load(skills, skill)
    except Exception:
        logger.exception("load_skill: could not prepare {skill}", skill=name)
        result = _result(False, f"The skill {name!r} could not be loaded.", None)
        await params.result_callback(result)
        return result

    await apply_load(prepared, skills, call_context, params)
    # After the switch, never inside it: `apply_load` must not suspend, and
    # recording is I/O. Spawned in the same step, so the load that happened
    # is the load recorded even if the caller interrupts from here on.
    record_skill_events(
        call_context,
        [
            *(
                SkillEvent(CallEventType.SKILL_UNLOADED, s, UNLOADED_FOR_LIMIT)
                for s in prepared.unloaded
            ),
            SkillEvent(CallEventType.SKILL_LOADED, skill, LOADED_BY_MODEL),
        ],
    )
    logger.info(
        "load_skill: loaded {skill} v{version}, unloaded {unloaded}",
        skill=name,
        version=skill.version,
        unloaded=[s.name for s in prepared.unloaded],
    )
    result = _result(
        True,
        f"The skill {name!r} is loaded: follow its instructions, and its tools "
        "are now available."
        + _unloaded_note(prepared.unloaded, skills.cap)
        + " Continue the conversation.",
        {
            "skill": skill.name,
            "version": skill.version,
            **(
                {"unloaded": [s.name for s in prepared.unloaded]}
                if prepared.unloaded
                else {}
            ),
        },
    )
    await params.result_callback(result, properties=_run_llm_after_switch(params))
    return result


def _unloaded_note(unloaded: tuple[ResolvedSkill, ...], cap: int) -> str:
    if not unloaded:
        return ""
    names = ", ".join(repr(s.name) for s in unloaded)
    return (
        f" At most {cap} skills can be loaded at once, so {names} was unloaded: "
        "load it again if you need it."
    )


def _run_llm_after_switch(params: Any) -> Any:
    from pipecat.frames.frames import FunctionCallResultProperties, LLMRunFrame

    async def run_llm() -> None:
        await params.pipeline_worker.queue_frame(LLMRunFrame())

    return FunctionCallResultProperties(run_llm=False, on_context_updated=run_llm)
