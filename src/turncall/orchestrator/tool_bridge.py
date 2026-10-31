"""Bridge between TurnCall tool definitions and Pipecat LLM function calling.

Converts ToolDefinition objects from AgentConfig into registered
function handlers on Pipecat's LLM service. Built-in tools delegate
to the shared call_control service for consistent behavior with
the live call control API.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import time
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import UUID

from loguru import logger

from turncall.domain.enums import ToolExecutionMode
from turncall.domain.models import BUILTIN_TOOL_NAMES, ToolDefinition
from turncall.domain.procedures import MOVE_TO_STEP
from turncall.orchestrator.procedure_escalation import run_move
from turncall.orchestrator.procedures import (
    leave_procedures,
    running_procedures,
    with_step_followup,
)
from turncall.services import call_control
from turncall.services.skills import LOAD_SKILL
from turncall.services.template_renderer import (
    prepend_knowledge_context,
    render_agent_config,
)
from turncall.services.tool_mocks import intercept
from turncall.services.tool_webhook import (
    invocation_record,
    post_tool_webhook,
    tool_text,
)

if TYPE_CHECKING:
    from pipecat.services.llm_service import LLMService

    from turncall.domain.models import AgentConfig
    from turncall.orchestrator.pipeline_factory import CallContext


# Background logging/dispatch tasks kept referenced so asyncio (which holds only
# a weak ref) doesn't GC them mid-flight.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> None:  # type: ignore[no-untyped-def]
    """Run a coroutine off the tool-result critical path."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _log_tool_result(
    call_context: CallContext,
    function_name: str,
    args: dict[str, Any],
    result: str,
    latency_ms: int | None = None,
) -> None:
    """Persist the tool invocation + call event and dispatch tool.result to
    webhook subscribers. Runs in the background; failures are logged, not raised."""
    try:
        from turncall.domain.enums import CallEventType
        from turncall.events.emit import emit_call_event, redact
        from turncall.storage.repositories import tool_invocation_repo

        # Before the session: no detector wait inside a transaction (events/emit).
        payload = await redact(
            call_context.project_id,
            CallEventType.TOOL_RESULT,
            {"tool_name": function_name, "arguments": args, "result": result},
            sessions=call_context.session_factory,
        )
        status, input_json, output_json = await invocation_record(
            call_context.project_id,
            args,
            result,
            sessions=call_context.session_factory,
        )
        async with call_context.session_factory() as session:
            await tool_invocation_repo.create_invocation(
                session,
                call_id=call_context.call_id,
                tool_name=function_name,
                input_json=input_json,
                status=status,
                output_json=output_json,
                latency_ms=latency_ms,
            )
            await emit_call_event(
                session,
                call_id=call_context.call_id,
                project_id=call_context.project_id,
                event_type=CallEventType.TOOL_RESULT,
                payload=payload,
                commit=True,  # the invocation row and the event, together
            )
    except Exception:
        logger.exception("tool_event_log_error")


async def _execute_webhook_tool(
    tool_def: ToolDefinition,
    args: dict[str, Any],
    call_context: CallContext,
) -> str:
    """Execute a webhook-based tool by POSTing to the configured URL."""
    return await post_tool_webhook(
        tool_def,
        args,
        project_id=call_context.project_id,
        call_id=call_context.call_id,
    )


async def _execute_builtin(
    function_name: str,
    args: dict[str, Any],
    call_context: CallContext,
) -> str:
    """Execute a built-in tool using shared call_control primitives."""
    if not call_context.has_database:
        return _builtin_without_a_platform(function_name)
    async with call_context.session_factory() as session:
        if function_name == "end_call":
            result = await call_control.begin_assistant_end(
                session,
                call_context.call_id,
                reason=args.get("reason", "assistant_ended"),
            )
        elif function_name == "transfer_call":
            result = await call_control.transfer_call(
                session,
                call_context.call_id,
                target_number=args.get("target_number", ""),
                transfer_mode=args.get("transfer_mode", "cold"),
                transfer_message=args.get("transfer_message"),
                briefing=args.get("briefing"),
                fallback_message=args.get("fallback_message"),
                reason=args.get("reason"),
            )
        elif function_name == "handoff_to_agent":
            target_id = args.get("agent_id") or args.get("assistant_id") or ""
            logger.info(
                "handoff_to_agent: target_id={target_id} args={args}",
                target_id=target_id,
                args=args,
            )
            resolved_target = UUID(target_id) if target_id else call_context.agent_id
            result = await call_control.handoff_to_agent(
                session,
                call_context.call_id,
                target_agent_id=resolved_target,
                reason=args.get("reason"),
                context_payload=args.get("context"),
            )
            logger.info(
                "handoff_to_agent: result success={success} message={msg}",
                success=result.success,
                msg=result.message,
            )
        elif function_name == "send_dtmf":
            result = await call_control.send_dtmf(
                session,
                call_context.call_id,
                digits=args.get("digits", ""),
            )
        elif function_name == "do_not_call":
            result = await call_control.do_not_call(session, call_context.call_id)
        else:
            return json.dumps({"error": f"Unknown built-in tool: {function_name}"})

        await session.commit()
        return json.dumps(
            {
                "success": result.success,
                "message": result.message,
                "details": result.details,
            }
        )


def _builtin_without_a_platform(function_name: str) -> str:
    """A built-in on a call with no database behind it — the website demo.

    `end_call` has nothing to record, so it only asks for the goodbye, and the
    hangup gate ends the call after it as on any call. The others act on a
    call row, a carrier or another agent, none of which exist here.
    """
    if function_name == "end_call":
        result = call_control.ControlResult(
            success=True, message=call_control.ASSISTANT_END_MESSAGE
        )
    else:
        result = call_control.ControlResult(
            success=False, message=f"{function_name} is not available on this call."
        )
    return json.dumps(
        {"success": result.success, "message": result.message, "details": None}
    )


def _with_followup(params: Any, call_context: CallContext, name: str) -> Any:
    """`params` whose result callback adds the procedure's reminder
    (`with_step_followup`) to whatever the tool answers, on every path:
    a mock, and the tools that deliver their own result (`end_call`,
    `load_skill`) too. The recorded result stays the tool's own."""
    answer = params.result_callback

    async def followed(result: Any, *rest: Any, **kwargs: Any) -> None:
        await answer(with_step_followup(result, call_context, name), *rest, **kwargs)

    # A copy, not `dataclasses.replace`: the suite drives handlers with
    # stand-in params (a namespace), and the original stays untouched either way.
    followed_params = copy.copy(params)
    followed_params.result_callback = followed
    return followed_params


def recorded_tool(name: str, handler: Any, call_context: CallContext) -> Any:
    """A tool registered outside `register_tools` — `query_knowledge` — logged
    and recorded as one of them: a `tool_invocations` row and `tool.result`,
    and mocked in an eval like them (#71). Without it a knowledge search on a
    call left no trace anywhere but an LLM trace span, and read as the agent
    never having looked."""

    async def run(params: Any) -> None:
        logger.info(
            "Tool called: {tool} for call {call_id}",
            tool=name,
            call_id=str(call_context.call_id),
        )
        args = dict(params.arguments)
        followed_params = _with_followup(params, call_context, name)
        # Ahead of the search, as `register_tools` does: under `mock_only` an
        # unmocked search is refused, never run against the real knowledge base.
        mocked = intercept(call_context.tool_mocks, name, args)
        if mocked is not None:
            await followed_params.result_callback(mocked)
            return
        started = time.perf_counter()

        async def deliver(result: Any, *rest: Any, **kwargs: Any) -> None:
            await followed_params.result_callback(result, *rest, **kwargs)
            text = tool_text(result)
            if call_context.tool_mocks is not None:
                # A `live` eval: the search really ran, and the run says so.
                call_context.tool_mocks.ran(name, args, text)
            if not call_context.records:
                return  # no `calls` row behind an eval (ADR-0018) or the demo
            latency_ms = int((time.perf_counter() - started) * 1000)
            _spawn(_log_tool_result(call_context, name, args, text, latency_ms))

        await handler(dataclasses.replace(params, result_callback=deliver))

    return run


# Built-ins that end the call they run on. They record the end and leave the
# hangup to the pipeline, so the goodbye is heard: see `orchestrator/hangup.py`.
ENDING_TOOL_NAMES = frozenset({"end_call", "do_not_call"})


def _ends_the_call(result: str) -> bool:
    """Whether an ending built-in's result says the call may now be ended.

    `do_not_call` succeeds on the opt-out alone, and says separately whether
    the end was recorded; a refused end leaves the call running.
    """
    try:
        payload = json.loads(result)
    except ValueError:
        return False
    details = payload.get("details") or {}
    return bool(payload.get("success")) and details.get("call_ended", True) is True


async def _run_ending_tool(
    function_name: str,
    args: dict[str, Any],
    call_context: CallContext,
    params: Any,
    hangup_gate: Any = None,
) -> str:
    """Record the end and arm the hangup, then let the model say goodbye.

    The result goes back as usual, so the model answers it — that answer is
    the goodbye, and `HangupAfterReply` ends the call once it has played. The
    arm frame goes first: the result can be dropped by a cancellation, and
    the record is already written.

    `hangup_gate` is the call's gate itself, for an LLM outside the call's
    pipeline — an `openai_live` client delegate in its own worker (#245),
    where pushing from `params.llm` would arm nothing. That delegate's
    answer is relayed by gpt-live-1, which may say something meanwhile, so
    the tool only marks the call as ending, and the live service arms the
    gate as it relays the goodbye (#262, `delegation_events`).
    """
    from turncall.orchestrator.hangup import HangupArmFrame

    result = await _execute_builtin(function_name, args, call_context)
    if _ends_the_call(result):
        if hangup_gate is not None:
            hangup_gate.begin_ending()
        else:
            await params.llm.push_frame(HangupArmFrame())
    await params.result_callback(result)
    return result


def _mark_uninterruptible(frame: Any) -> Any:
    """Mark a frame as one an interruption must not drop.

    A handoff queues the target's prompt and the target's advertised tools.
    Pipecat drains interruptible frames from a processor's queue when the
    caller barges in, so losing either leaves the agent running the previous
    agent's prompt and tools over a context that was already cleared —
    silently. The flag makes the interruption land before the handoff or after
    it, never through the middle. Pipecat 1.12 shipped the same fix for Flows.

    Narrow by design: spoken frames (the first message, the idle nudge) are
    *supposed* to be dropped when the caller starts talking.

    Mutates the frame, against this repo's immutability rule, because pipecat
    declares ``interruptible`` as a non-init dataclass field — there is no
    constructor argument to pass it to. The alternative, ``UninterruptibleFrame``,
    is the pre-1.12 marker: deprecated, removed in 2.0, and subclass-based, so
    it would mean declaring TurnCall frame subclasses purely to set a flag. The
    frame is freshly built at the call site and not yet queued, so nothing else
    can observe the mutation.
    """
    frame.interruptible = False
    return frame


class PreparedHandoff(NamedTuple):
    """Everything a handoff needs, assembled before anything is committed.

    Carrying the built frames rather than just the config is the point: the
    switch is committed to the database *before* the pipeline is touched, so
    anything that can wait or raise has to have happened already. `skills`
    is the target agent's (`skill_loader.CallSkills`, none loaded), resolved
    here for the same reason and swapped into the call by the apply step
    (#427); `config` is then the target's with its skills' list and
    `load_skill`, and its procedures' `move_to_step`: `procedures` (#448).
    """

    name: str
    config: AgentConfig
    frames: tuple[Any, ...]
    skills: Any | None = None
    procedures: Any | None = None


@dataclass(frozen=True)
class SwitchInputs:
    """What the call's prompt and tools were built from besides its config,
    for a switch that keeps them: the knowledge preamble, the tools discovered
    or added at build (MCP, then `query_knowledge`), and whether the LLM is a
    BYOM one that reads its tools from the prompt instead.

    A handoff passes none: the target agent's prompt and tools are its own,
    and MCP is not reconnected mid-call. A skill load (#424) passes the
    call's, so loading a skill takes nothing away.
    """

    knowledge_preamble: str = ""
    extra_tools: tuple[ToolDefinition, ...] = ()
    byom: bool = False


# A handoff's: the target agent's config and nothing else.
_NO_INPUTS = SwitchInputs()


def build_switch_frames(
    config: AgentConfig, inputs: SwitchInputs = _NO_INPUTS
) -> tuple[Any, Any]:
    """Build the two frames that switch the call to `config`'s prompt and
    tools, marked uninterruptible.

    Called before anything is committed, where raising is still safe. A
    handoff builds them for its target agent; nothing here is handoff-specific.
    `inputs` are built the way `create_pipeline` builds them, so a switch that
    passes the call's own reproduces the call's prompt and tools around
    `config`'s.
    """
    from pipecat.frames.frames import LLMSetToolsFrame, LLMUpdateSettingsFrame
    from pipecat.processors.aggregators.llm_context import NOT_GIVEN
    from pipecat.services.llm_service import LLMSettings

    from turncall.orchestrator.pipeline_factory import (
        _build_system_instruction,
        _build_tools_schema,
    )

    # The prompt lives on the LLM service, not in the context, so switching
    # agents is a settings update rather than a rewritten first message. Doing
    # it the old way would send both prompts: the OpenAI adapter prepends
    # system_instruction to the context messages, so the previous agent's
    # instructions would survive the handoff.
    system_instruction = _build_system_instruction(
        config,
        inject_tools_prompt=inputs.byom,
        knowledge_preamble=inputs.knowledge_preamble,
    )
    prompt_frame = _mark_uninterruptible(
        LLMUpdateSettingsFrame(
            delta=LLMSettings(system_instruction=system_instruction),
            # A failover backup (#222) must take over as the new agent too.
            reach_inactive_services=True,
        )
    )
    # Tools live in two places: the handler registry and the advertised
    # schema. Moving only the prompt left the model believing it was the new
    # agent while still holding the previous one's tools, and none of its own.
    # NOT_GIVEN clears the set when the target defines none, and for a BYOM
    # LLM, whose tools are in its prompt and never advertised (as at build).
    tools_schema = (
        None if inputs.byom else _build_tools_schema(config, list(inputs.extra_tools))
    )
    tools_frame = _mark_uninterruptible(
        LLMSetToolsFrame(tools=tools_schema if tools_schema is not None else NOT_GIVEN)
    )
    return prompt_frame, tools_frame


async def _as_this_call(
    session: Any, call_id: UUID, config: AgentConfig
) -> AgentConfig:
    """The target agent with what the call was started with.

    Every voice entry point stores the call's `{{variables}}` and call-init's
    `knowledge_context` on the call row; the agent the call started as got
    both at build. Applied in the media stream's order — render, then
    prepend — so the target's prompt reads as the first agent's did. An eval
    has no row, so neither.
    """
    from turncall.storage.repositories import call_repo

    call = await call_repo.get_call_by_id(session, call_id)
    metadata = (call.metadata_json or {}) if call else {}
    variables = metadata.get("template_variables")
    rendered = render_agent_config(
        config, variables if isinstance(variables, dict) else {}
    )
    knowledge_context = metadata.get("knowledge_context")
    if not isinstance(knowledge_context, str):
        return rendered
    return prepend_knowledge_context(rendered, knowledge_context)


async def _prepare_handoff(
    args: dict[str, Any],
    call_context: CallContext,
) -> PreparedHandoff | None:
    """Read the target agent and build its frames, before the switch commits.

    All of the handoff's I/O and all of its fallible work, deliberately
    separated from applying it. `call_control.handoff_to_agent` commits
    `status=HANDED_OFF` and `active_agent_id=<target>` before the pipeline is
    touched, so a DB round-trip *after* that commit is a cancellation window —
    pipecat cancels a sync tool's task on every interruption — and a raise
    after it is the same divergence with a deterministic trigger. Either way
    the record says handed off and the live pipeline never changes. #144.

    Returns None when the handoff cannot be prepared, having logged why; the
    caller then leaves the pipeline alone.
    """
    from turncall.domain.models import AgentConfig

    target_id = args.get("agent_id") or args.get("assistant_id") or ""
    if not target_id:
        return None

    try:
        async with call_context.session_factory() as session:
            from turncall.storage.repositories import agent_repo

            # The call's own project: an agent id is not a capability.
            target = await agent_repo.get_agent_by_id(
                session, UUID(target_id), project_id=call_context.project_id
            )
            if target is None:
                logger.warning("handoff_context: target agent not found")
                return None
            config = await _as_this_call(
                session,
                call_context.call_id,
                AgentConfig.model_validate(target.config_blob),
            )

        from turncall.orchestrator.skill_loader import prepare_handoff_skills

        # Before its skills, as at build: a skill load keeps `move_to_step`.
        config, procedures = running_procedures(config)
        # The target's skills, resolved as at a call's start: I/O, so here,
        # before the commit — never in the apply step (#427).
        skills, config = await prepare_handoff_skills(config, call_context)
        frames = build_switch_frames(config)
    except Exception:
        logger.exception("handoff_context: failed to prepare the handoff")
        return None

    if config.mcp_servers:
        # MCP sessions belong to the agent the call started as — they aren't
        # torn down and re-opened mid-call, so the target's servers stay
        # unconnected. Better said out loud than discovered.
        logger.warning(
            "handoff_context: target agent's mcp servers are not connected "
            "mid-call: {servers}",
            servers=[s.name for s in config.mcp_servers],
        )

    return PreparedHandoff(target.name, config, frames, skills, procedures)


async def apply_switch(
    config: AgentConfig,
    frames: tuple[Any, ...],
    call_context: CallContext,
    params: Any,
    *,
    clear_context: bool,
) -> None:
    """Switch the running pipeline to `config`'s prompt and tools. Atomically.

    `frames` are `build_switch_frames(config)`, built by the caller along with
    everything else that can wait or raise, before anything was committed.
    What is left cannot suspend — `queue_frame` puts onto an unbounded
    asyncio.Queue, which never yields — so an interruption is delivered either
    before this or after it, never through it.

    Whether the conversation so far survives is the caller's choice: a handoff
    clears it, since the target agent starts over.

    Adding an `await`, or anything that can raise, reopens the half-applied
    state the `interruptible=False` flag cannot reach: a frame that was never
    built cannot survive a drain. `test_handoff_switch_is_atomic.py` fails if
    it happens.
    """
    from turncall.orchestrator.llm_failover import FailoverLLM

    if clear_context:
        params.context.set_messages([])
    # Every member of a failover group (#222), not only the one answering.
    group = FailoverLLM.group_of(params.llm)
    drop_config_tools(group, keep=config.tools)
    if config.tools:
        register_tools(group, list(config.tools), call_context)
    remember_config_tools(group, config.tools)
    for frame in frames:
        await params.pipeline_worker.queue_frame(frame)


async def _apply_handoff_context(
    prepared: PreparedHandoff,
    call_context: CallContext,
    params: Any,
) -> None:
    """Switch the running pipeline to the target agent, over a cleared context.

    Everything that can wait or raise happened in `_prepare_handoff`, before
    the switch was committed; `apply_switch` is the part that must not
    suspend, and nothing here may either — the skills swap included (#427):
    from here the call's skills are the target agent's, none loaded, and
    so are its procedures (#448).
    """
    await apply_switch(
        prepared.config, prepared.frames, call_context, params, clear_context=True
    )
    holder = getattr(call_context, "skills", None)
    if holder is not None and prepared.skills is not None:
        holder.adopt(prepared.skills)
    leave_procedures(call_context, prepared.procedures)

    logger.info(
        "handoff_context: switched system instruction to agent '{name}'",
        name=prepared.name,
    )


async def _complete_handoff(
    prepared: PreparedHandoff,
    result: str,
    call_context: CallContext,
    params: Any,
) -> bool:
    """Apply a prepared handoff, but only if the switch was actually committed.

    Applying one the database refused would be #144 the other way up: the
    pipeline speaking as the target agent while the call record never moved.
    The payload is ours, built one frame up by `_execute_builtin`. Returns
    whether the pipeline switched.
    """
    try:
        committed = bool(json.loads(result).get("success"))
    except (ValueError, AttributeError):
        committed = False

    if not committed:
        logger.warning(
            "handoff_context: switch was refused, leaving the pipeline on the "
            "current agent: {result}",
            result=result,
        )
        return False

    await _apply_handoff_context(prepared, call_context, params)
    return True


class HandoffOutcome(NamedTuple):
    """`result` is the tool result's JSON — the model reads it, the API reads
    its message; `applied` is the switch the call made, None when it made none."""

    result: str
    applied: PreparedHandoff | None


# A handoff that could not be prepared is never committed: the record would
# say handed off while the pipeline stayed on the current agent. #144.
_NOT_PREPARED = json.dumps(
    {
        "success": False,
        "message": (
            "The target agent was not found in this project or could not be "
            "loaded; the call stays on the current agent"
        ),
        "details": None,
    }
)


async def run_handoff(
    args: dict[str, Any], call_context: CallContext, params: Any
) -> HandoffOutcome:
    """The one handoff: the model's `handoff_to_agent` call and the API's
    `POST /v1/calls/{id}/handoff` (#216) both run this, inside the call.

    Prepare, then commit, then apply — the order #144 depends on. `params` is
    anything with the call's `llm`, `context` and `pipeline_worker`: a tool
    call's `FunctionCallParams`, or what `live_control` found in the pipeline.
    """
    prepared = await _prepare_handoff(args, call_context)
    if prepared is None:
        return HandoffOutcome(_NOT_PREPARED, None)
    result = await _execute_builtin("handoff_to_agent", args, call_context)
    switched = await _complete_handoff(prepared, result, call_context, params)
    if switched and prepared.skills is not None and prepared.skills.available:
        from turncall.orchestrator.skill_loader import record_resolution

        # After the switch, off its path: the call read lists the target's
        # skill versions beside the first agent's (#427).
        _spawn(record_resolution(call_context, prepared.skills.available))
    return HandoffOutcome(result, prepared if switched else None)


def advertised_tool_names(prepared: PreparedHandoff) -> list[str]:
    """The tools a handoff's frame advertises: what the model now holds."""
    tools = getattr(prepared.frames[-1], "tools", None)
    return [t.name for t in getattr(tools, "standard_tools", None) or []]


# The config tools each LLM (or failover group) has handlers for, so a switch
# can take away the ones its new config no longer has: an unloaded skill's,
# the previous agent's on a handoff. MCP tools and `query_knowledge` are
# registered beside these, never in them, and a switch keeps both.
_CONFIG_TOOLS: weakref.WeakKeyDictionary[Any, frozenset[str]] = (
    weakref.WeakKeyDictionary()
)


def remember_config_tools(llm: Any, tools: Sequence[ToolDefinition]) -> None:
    """Record the config tools `llm` now has handlers for."""
    _CONFIG_TOOLS[llm] = frozenset(t.name for t in tools)


def drop_config_tools(llm: Any, *, keep: Sequence[ToolDefinition]) -> None:
    """Unregister the config tools `llm` has handlers for that `keep` lacks.

    Pipecat drops a handler on its own only for a tool it registered itself
    from the advertised schema; these were registered by name, so a tool the
    switch stops advertising would still run if the model called it (its
    earlier call is in the kept context). A call to one now gets pipecat's
    "not available" result. Cannot raise or suspend: it runs inside
    `apply_switch`.
    """
    kept = {t.name for t in keep}
    gone = _CONFIG_TOOLS.get(llm, frozenset()) - kept
    for member in getattr(llm, "llms", None) or [llm]:
        for name in gone:
            if member.has_function(name):
                member.unregister_function(name)


def register_tools(
    llm: LLMService,
    tools: list[ToolDefinition],
    call_context: CallContext,
    hangup_gate: Any = None,
) -> None:
    """Register tool definitions as LLM function handlers.

    Deduplicated by name, first wins — the same rule and the same order
    `_build_tools_schema` applies when it decides what to advertise. Pipecat's
    registry is keyed by name, so without this a duplicate silently replaced
    the handler for a name the schema had already given to someone else.
    """
    registered = 0
    seen: set[str] = set()
    for tool_def in tools:
        if tool_def.name in seen:
            logger.warning("tool_registration_collision_skipped", tool=tool_def.name)
            continue
        seen.add(tool_def.name)
        try:
            _register_single_tool(llm, tool_def, call_context, hangup_gate)
            registered += 1
        except Exception:
            logger.exception("Failed to register tool: {name}", name=tool_def.name)
    logger.info("Tools registered: {n}/{total}", n=registered, total=len(tools))


def _register_single_tool(
    llm: LLMService,
    tool_def: ToolDefinition,
    call_context: CallContext,
    hangup_gate: Any = None,
) -> None:
    """Register a single tool on the LLM service."""
    from pipecat.services.llm_service import FunctionCallParams

    async def handler(params: FunctionCallParams) -> None:
        function_name = params.function_name
        args = params.arguments

        logger.info(
            "Tool called: {tool} for call {call_id}",
            tool=function_name,
            call_id=str(call_context.call_id),
        )
        followed_params = _with_followup(params, call_context, function_name)

        # An eval's mocks short-circuit ahead of every branch below: a built-in
        # acts on a call that does not exist, a webhook books a real
        # appointment, an MCP server is a third party's. `intercept` returns
        # None on a real call (no mocks) and under `live`, so this costs a
        # dict lookup there. #71.
        mocked = intercept(call_context.tool_mocks, function_name, args)
        if mocked is not None:
            await followed_params.result_callback(mocked)
            return

        started = time.perf_counter()
        delivered = False
        if function_name in ENDING_TOOL_NAMES and not call_context.is_eval:
            # Shielded as one unit: a built-in is cancelled when the caller
            # talks over the goodbye, and pipecat cancels it even after its
            # result is in. A cancel between recording the end and ending the
            # pipeline left the row saying the agent hung up on a call that
            # kept running. The arm frame is uninterruptible for the same
            # reason, so this only makes the tool agree with it. (An eval has
            # no call to end, and the harness owns its length.)
            result = await asyncio.shield(
                _run_ending_tool(
                    function_name,
                    dict(args),
                    call_context,
                    followed_params,
                    hangup_gate,
                )
            )
            delivered = True
        elif function_name == "handoff_to_agent":
            # Prepared *before* `_execute_builtin` commits `status=HANDED_OFF` +
            # `active_agent_id`, then applied without waiting on anything:
            # anything that waits or raises after that commit is a window where
            # the record says handed off and the pipeline never switched. #144.
            result = (
                await run_handoff(dict(args), call_context, followed_params)
            ).result
        elif function_name == LOAD_SKILL:
            # Adds a skill's prompt section and tools without clearing the
            # context, and asks for the model's next inference itself, after
            # the switch (#424): so it delivers its own result.
            from turncall.orchestrator.skill_loader import run_load_skill

            result = await run_load_skill(dict(args), call_context, followed_params)
            delivered = True
        elif function_name == MOVE_TO_STEP:
            result = await run_move(dict(args), call_context, followed_params)
        elif function_name in BUILTIN_TOOL_NAMES:
            execution = _execute_builtin(function_name, args, call_context)
            # A transfer takes the call away from this pipeline: the carrier
            # closes the media stream as part of it, which cancels this tool
            # mid-flight. Unshielded, `call.transferred` and the status were
            # lost with it and the call read as the caller hanging up.
            result = await (
                asyncio.shield(execution)
                if function_name == "transfer_call"
                else execution
            )
        elif (
            # Only when this registration is not itself a webhook tool. Asking
            # the manager by name alone sent a name the agent config had
            # already claimed to the MCP server instead: the model was shown
            # the customer's description and schema, and something else ran.
            # _build_tools_schema gives static tools the name, so dispatch has
            # to agree. chat_tools._run makes the same check.
            not tool_def.webhook_url
            and call_context.mcp_manager is not None
            and call_context.mcp_manager.is_mcp_tool(function_name)
        ):
            result = await call_context.mcp_manager.call_tool(function_name, args)
        else:
            result = await _execute_webhook_tool(tool_def, args, call_context)

        # Hand the result back to the LLM immediately — the model is waiting on
        # this to continue speaking. The invocation record + tool.result webhook
        # dispatch (which can retry ~90s against a dead subscriber) run off the
        # critical path so they never sit between the caller and the response.
        latency_ms = int((time.perf_counter() - started) * 1000)
        if not delivered:
            await followed_params.result_callback(result)
        if call_context.tool_mocks is not None:
            # The tool really ran — a `live` eval, or `load_skill`, which an
            # eval never mocks (#431) — and the run's record says so.
            call_context.tool_mocks.ran(function_name, args, result)
        if call_context.records:
            # No `calls` row behind an eval (ADR-0018), so
            # `tool_invocations.call_id` has nothing to point at and the write
            # is a foreign-key error every time. The iteration's tool record is
            # `ToolMocks.calls` instead, which is what reaches the run's
            # results. Decided here rather than inside the coroutine so an eval
            # does not spawn a task per tool call to do nothing.
            _spawn(
                _log_tool_result(call_context, function_name, args, result, latency_ms)
            )

    # execution_mode="async" means the call outlives an interruption: its
    # result is delivered whenever it arrives instead of being cancelled the
    # moment the caller talks over the agent. Pipecat spells that
    # cancel_on_interruption=False, and its own _function_is_async() reads the
    # same flag back. Sync stays the default — a built-in acting on the call
    # itself must not survive the caller changing their mind.
    llm.register_function(
        tool_def.name,
        handler,
        cancel_on_interruption=tool_def.execution_mode != ToolExecutionMode.ASYNC,
    )
