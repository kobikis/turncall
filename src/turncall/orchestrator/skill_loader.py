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

A skill's knowledge bases (#429) are read when it resolves: a load adds its
`prompt`-mode documents to its section and makes its `tool`-mode KBs
searchable through `query_knowledge` — the agent's own if it has one, else
one the load adds with its hint — and an unload takes both away again. The
handler is registered in the load's apply step (a plain assignment in
pipecat's registry) and reads which KBs to search when it is called, from
this holder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NamedTuple

from loguru import logger

from turncall.domain.enums import CallEventType
from turncall.domain.models import BUILTIN_TOOL_NAMES
from turncall.orchestrator.config_warnings import skills_on_s2s_warning
from turncall.services.retrieval import (
    KNOWLEDGE_AWARENESS_HINT,
    KNOWLEDGE_TOOL_HINT,
    KNOWLEDGE_TOOL_SCHEMA,
)
from turncall.services.skills import ResolvedSkill, resolve_skills, skilled_config

QUERY_KNOWLEDGE = KNOWLEDGE_TOOL_SCHEMA["name"]

if TYPE_CHECKING:
    from turncall.domain.models import AgentConfig
    from turncall.orchestrator.knowledge_processor import KnowledgeTool
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

    `own_knowledge` are the agent's own tool-mode KB attachments, which the
    switch's `query_knowledge` searches (none after a handoff, whose switch
    keeps no discovered tools); `embedding_key` is the call's OpenAI key, for
    the query's embedding (#429).
    """

    base: AgentConfig
    available: tuple[ResolvedSkill, ...]
    switch: SwitchInputs
    loaded: tuple[ResolvedSkill, ...] = ()
    own_knowledge: tuple[dict[str, Any], ...] = ()
    # A key: never in a repr, since the call's context (which holds this)
    # may be.
    embedding_key: str = field(default="", repr=False)

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
        self.own_knowledge = other.own_knowledge
        self.embedding_key = other.embedding_key

    def config(self, loaded: tuple[ResolvedSkill, ...] | None = None) -> AgentConfig:
        """The config the model runs on with `loaded` (default: what is)."""
        return skilled_config(
            self.base, self.available, self.loaded if loaded is None else loaded
        )

    def searched(self) -> list[dict[str, Any]]:
        """What `query_knowledge` searches now: the agent's own tool-mode KBs,
        then each loaded skill's, in load order, each KB once."""
        attachments = [
            *self.own_knowledge,
            *(a for s in self.loaded for a in s.knowledge.search),
        ]
        seen: set[str] = set()
        unique = []
        for a in attachments:
            if str(a["knowledge_base_id"]) not in seen:
                seen.add(str(a["knowledge_base_id"]))
                unique.append(a)
        return unique

    def knowledge_switch(self, loaded: tuple[ResolvedSkill, ...]) -> SwitchInputs:
        """The switch's inputs with `loaded`: the call's own, plus — when a
        loaded skill has tool-mode KBs and the call offers no
        `query_knowledge` yet — that tool, last, and the hints an agent's own
        tool-mode KB puts in the preamble (those already there kept once)."""
        skill_search = [a for s in loaded for a in s.knowledge.search]
        offered = {t.name for t in self.switch.extra_tools}
        if not skill_search or QUERY_KNOWLEDGE in offered:
            return self.switch
        from turncall.orchestrator.knowledge_processor import (
            knowledge_tool_definition,
        )

        definition = knowledge_tool_definition(skill_search)
        if definition is None:  # every entry is tool-mode; for the type checker
            return self.switch
        preamble = self.switch.knowledge_preamble
        hints = [
            h
            for h in (KNOWLEDGE_AWARENESS_HINT, KNOWLEDGE_TOOL_HINT)
            if h not in preamble
        ]
        return replace(
            self.switch,
            knowledge_preamble="\n\n".join(p for p in (preamble, *hints) if p),
            extra_tools=(*self.switch.extra_tools, definition),
        )


def _knowledge_taken(config: AgentConfig, call_context: Any) -> bool:
    """Whether a tool of the agent's or a loaded skill's — or an MCP server's
    — has the name `query_knowledge`: the first of a name wins, so the KBs'
    search is not offered under it."""
    if any(t.name == QUERY_KNOWLEDGE for t in config.tools):
        return True
    mcp = getattr(call_context, "mcp_manager", None)
    return mcp is not None and bool(mcp.is_mcp_tool(QUERY_KNOWLEDGE))


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
    pinned or live published versions, the call's project and
    `{{variables}}`), none
    loaded — and the config the call then runs: the target's own with the
    list and `load_skill`, or unchanged for a target with none.

    I/O, so it runs in the handoff's prepare step, before the switch commits.
    A handoff's switch keeps none of the call's preamble or discovered tools
    (`SwitchInputs()`), so neither does a later load on the target.
    """
    from turncall.orchestrator.tool_bridge import SwitchInputs

    available = await resolve_call_skills(config, call_context)
    current = getattr(call_context, "skills", None)
    skills = CallSkills(
        base=config,
        available=available,
        switch=SwitchInputs(),
        embedding_key=getattr(current, "embedding_key", ""),
    )
    return skills, skills.config()


async def resolve_call_skills(
    config: AgentConfig, call_context: CallContext
) -> tuple[ResolvedSkill, ...]:
    """The agent's skills as this call runs them, or none.

    Cascade voice only: a realtime session reads no system or tool change
    made after it starts on most providers, so an S2S agent's skills are
    skipped with a warning (the agent read carries the same, #428). A call with
    no database behind it — the website demo — has no project to resolve a
    skill in. Nothing here can fail the call: a skill that does not resolve is
    a warning and a skipped skill.
    """
    if not config.skills:
        return ()
    on_s2s = skills_on_s2s_warning(config)
    if on_s2s is not None:
        logger.warning("skills_skipped: {message}", message=on_s2s["message"])
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
    `[{id, skill, version, pinned}]` (`pinned`, #426) under `metadata_json["resolved_skills"]` — at
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
        {"id": str(s.id), "skill": s.name, "version": s.version, "pinned": s.pinned}
        for s in available
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
    skills it takes off to stay within the cap, longest loaded first;
    `knowledge` the `query_knowledge` handler to register, when the loaded
    skills have tool-mode KBs to search (#429)."""

    skill: ResolvedSkill
    loaded: tuple[ResolvedSkill, ...]
    config: AgentConfig
    frames: tuple[Any, ...]
    unloaded: tuple[ResolvedSkill, ...] = ()
    knowledge: KnowledgeTool | None = None


def prepare_load(
    skills: CallSkills, skill: ResolvedSkill, call_context: Any = None
) -> PreparedLoad:
    """The config and both frames for `skill` loaded — and, past the cap, the
    skills loaded longest ago unloaded, in the same config and frames (#427),
    so the unload and the load are one switch. Pure, and may raise: nothing
    has been applied yet. A skill already loaded is answered before this
    (`run_load_skill`); here it would stay where it is.

    With the call's context, the loaded skills' tool-mode KBs are offered
    through `query_knowledge` (#429): its handler is built here, registered
    by `apply_load`."""
    from turncall.orchestrator.tool_bridge import build_switch_frames

    if skills.is_loaded(skill.name):
        held = skills.loaded
    else:
        held = (*skills.loaded, skill)
    over = max(len(held) - skills.cap, 0)
    unloaded, loaded = held[:over], held[over:]
    config = skills.config(loaded)
    knowledge = _knowledge_for(skills, loaded, config, call_context)
    switch = skills.knowledge_switch(loaded) if knowledge else skills.switch
    return PreparedLoad(
        skill,
        loaded,
        config,
        build_switch_frames(config, switch),
        unloaded,
        knowledge,
    )


def _knowledge_for(
    skills: CallSkills,
    loaded: tuple[ResolvedSkill, ...],
    config: AgentConfig,
    call_context: Any,
) -> KnowledgeTool | None:
    """The handler that searches the agent's tool-mode KBs and the loaded
    skills' — None when no loaded skill has one, or the name is taken."""
    if call_context is None or not any(s.knowledge.search for s in loaded):
        return None
    if _knowledge_taken(config, call_context):
        logger.warning(
            "skill_knowledge_not_offered: a tool named query_knowledge is "
            "already the call's, so the loaded skills' knowledge bases are not "
            "searchable"
        )
        return None
    from turncall.orchestrator.knowledge_processor import searching_knowledge_tool

    # The holder's own `searched`, read when the model calls the tool: after
    # this load is applied that is `loaded`'s; after a later unload, less.
    return searching_knowledge_tool(
        skills.searched,
        [*skills.own_knowledge, *(a for s in loaded for a in s.knowledge.search)],
        call_context.session_factory,
        skills.embedding_key,
        call_context,
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
    if prepared.knowledge is not None:
        from turncall.orchestrator.llm_failover import FailoverLLM

        # Every member of a failover group, as `apply_switch` registers tools.
        # It replaces the build's handler for the agent's own KBs: this one
        # searches those too.
        FailoverLLM.group_of(params.llm).register_function(
            prepared.knowledge.definition.name, prepared.knowledge.handler
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
        prepared = prepare_load(skills, skill, call_context)
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
