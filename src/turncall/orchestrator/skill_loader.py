"""Loading a skill on a voice call (#424).

At build, `resolve_call_skills` resolves the agent's skills once, against the
call's project, and the call runs `CallSkills.config()`: the agent's config
with the list of skills in its prompt and `load_skill` among its tools. The
model's `load_skill` then runs `run_load_skill`, which follows the handoff's
prepare-then-apply discipline (#144) over the same switch (#422): the new
config and both frames are built first, where raising is still harmless, and
applied with nothing left to wait on — but, unlike a handoff, over the context
as it is, so the conversation so far is kept.

`CallSkills.available` is what the call resolved, versions included, for the
call record (#425); `loaded` is what it has loaded, in load order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from loguru import logger

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
    is the one thing about it that changes mid-call. `loaded` is replaced
    whole, never appended to, and only by `apply_load`.
    """

    base: AgentConfig
    available: tuple[ResolvedSkill, ...]
    switch: SwitchInputs
    loaded: tuple[ResolvedSkill, ...] = ()

    def find(self, name: str) -> ResolvedSkill | None:
        return next((s for s in self.available if s.name == name), None)

    def config(self, loaded: tuple[ResolvedSkill, ...] | None = None) -> AgentConfig:
        """The config the model runs on with `loaded` (default: what is)."""
        return skilled_config(
            self.base, self.available, self.loaded if loaded is None else loaded
        )


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


class PreparedLoad(NamedTuple):
    """A load with everything built and nothing applied."""

    skill: ResolvedSkill
    loaded: tuple[ResolvedSkill, ...]
    config: AgentConfig
    frames: tuple[Any, ...]


def prepare_load(skills: CallSkills, skill: ResolvedSkill) -> PreparedLoad:
    """The config and both frames for `skill` loaded. Pure, and may raise:
    nothing has been applied yet. A skill already loaded is loaded as it is
    (#427 answers it instead)."""
    from turncall.orchestrator.tool_bridge import build_switch_frames

    loaded = skills.loaded if skill in skills.loaded else (*skills.loaded, skill)
    config = skills.config(loaded)
    return PreparedLoad(
        skill, loaded, config, build_switch_frames(config, skills.switch)
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

    try:
        prepared = prepare_load(skills, skill)
    except Exception:
        logger.exception("load_skill: could not prepare {skill}", skill=name)
        result = _result(False, f"The skill {name!r} could not be loaded.", None)
        await params.result_callback(result)
        return result

    await apply_load(prepared, skills, call_context, params)
    logger.info(
        "load_skill: loaded {skill} v{version}", skill=name, version=skill.version
    )
    result = _result(
        True,
        f"The skill {name!r} is loaded: follow its instructions, and its tools "
        "are now available. Continue the conversation.",
        {"skill": skill.name, "version": skill.version},
    )
    await params.result_callback(result, properties=_run_llm_after_switch(params))
    return result


def _run_llm_after_switch(params: Any) -> Any:
    from pipecat.frames.frames import FunctionCallResultProperties, LLMRunFrame

    async def run_llm() -> None:
        await params.pipeline_worker.queue_frame(LLMRunFrame())

    return FunctionCallResultProperties(run_llm=False, on_context_updated=run_llm)
