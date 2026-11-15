"""Running a call: what every voice edge does once it has opened one (#491).

An edge verifies its request, opens the call and answers in its own format;
it hands the call here with a factory for its transport. Running owns the
rest, once for every edge:

- the drain count (#199), so a SIGTERM waits for the call;
- the live-call slot (#224), held and renewed while the call runs and freed
  however it ends, a failed build included;
- the agent's MCP servers, connected before the build and closed whatever
  happens — a build that raises, a cancellation before the start, the end of
  the call (where `CallSession` closes them) — in the task that opened them,
  which an MCP transport's anyio cancel scopes require;
- the ambience mixer, at the edge's rate, handed to the edge's factory;
- the call context, with the inline-agent sentinel (ADR-0017);
- the build (an accepted agent only, #481) and the start.

`own_task=True` is for an edge that returns from a connection callback while
the call goes on (WebRTC, WhatsApp voice): the run happens in a task kept
here, and `run_call` returns once the build succeeded — or raises what it
raised, so WebRTC can still answer `500 pipeline_start_failed`. Without it
the run blocks until the call ends (a carrier's media stream handler).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

from turncall.config import get_settings
from turncall.config.settings import Settings
from turncall.orchestrator.ambience import build_ambience_mixer
from turncall.orchestrator.call_session import CallSession, finalize_call
from turncall.orchestrator.pipeline_builder import build_call_pipeline
from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID, CallContext
from turncall.services import drain
from turncall.services.agent_acceptance import AcceptedAgent
from turncall.services.call_opening import OpenedCall
from turncall.services.limits import hold_live_call


@dataclass(frozen=True)
class CallToRun:
    """What running needs of an opened call.

    `agent` is acceptance's answer, already rendered with the call's
    variables and knowledge context. `agent_id` is the call row's — None for
    an inline agent, which has no row (ADR-0017). `call_sid`/`stream_sid` are
    the edge's handles for the call. `session_factory` None is a call with no
    database (the demo): nothing is recorded and no slot is held.
    """

    call_id: UUID
    project_id: UUID
    agent: AcceptedAgent
    agent_id: UUID | None
    call_sid: str
    stream_sid: str
    session_factory: Any | None

    @classmethod
    def of(
        cls,
        opened: OpenedCall,
        *,
        session_factory: Any | None,
        stream_sid: str | None = None,
    ) -> CallToRun:
        """The call `open_call` opened, to run: its agent rendered with the
        call's variables and knowledge context, named by the edge's handles
        (`stream_sid` when the stream has an id of its own)."""
        metadata = opened.metadata
        call_sid = opened.handles.call_sid
        return cls(
            call_id=opened.call_id,
            project_id=opened.project_id,
            agent=opened.agent.rendered(
                metadata.template_variables,
                knowledge_context=metadata.knowledge_context,
            ),
            agent_id=opened.agent.agent_id,
            call_sid=call_sid,
            stream_sid=stream_sid or call_sid,
            session_factory=session_factory,
        )


@dataclass(frozen=True)
class TransportFactory:
    """An edge's transport: `make(mixer=...)` builds it around the call's
    ambience mixer (None for no room), at `sample_rate`. `carries_video` is a browser
    call's, the only one that can show an Avatar."""

    make: Callable[..., Any]
    sample_rate: int
    carries_video: bool = False


class CallCancelledBeforeStart(RuntimeError):
    """The call's own task was cancelled before its pipeline was built."""


# Calls run in a task of their own, kept referenced so asyncio (which holds
# only a weak ref) can't GC a running call.
_RUNNING: set[asyncio.Task[None]] = set()


async def run_call(
    call: CallToRun,
    make_transport: TransportFactory,
    *,
    own_task: bool,
    settings: Settings | None = None,
) -> asyncio.Task[None] | None:
    """Run `call` on the transport `make_transport` builds (see the module
    docstring). With `own_task`, returns the task the call runs in once its
    pipeline is built; without, returns when the call ends. A build that
    raises is raised here either way, the call already failed with why."""
    settings = settings or get_settings()
    if not own_task:
        await _run(call, make_transport, settings, built=None)
        return None
    built: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_run(call, make_transport, settings, built=built))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    await built
    return task


async def _run(
    call: CallToRun,
    make_transport: TransportFactory,
    settings: Settings,
    *,
    built: asyncio.Future[None] | None,
) -> None:
    config = call.agent.config
    try:
        # Live until the session has finalized: a SIGTERM waits for it (#199).
        with drain.live_call(call.call_id, config.max_call_duration_seconds):
            async with _slot(call):
                session = await _build(call, make_transport, settings)
                if built is not None:
                    built.set_result(None)
                await session.start()
    except Exception as exc:
        if built is None or built.done():
            raise
        # The edge waits on `built` and nothing awaits this task: it is told
        # what went wrong, from the build or anything before it — once the
        # slot and the drain count are already given back.
        built.set_exception(exc)
    except BaseException:
        # A cancellation before the build answered: the edge is waiting.
        if built is not None and not built.done():
            built.set_exception(CallCancelledBeforeStart(str(call.call_id)))
        raise


@contextlib.asynccontextmanager
async def _slot(call: CallToRun) -> AsyncIterator[None]:
    """The slot taken when the call was opened (#224), this run's from here:
    renewed while it runs, freed however it ends. None with no database."""
    if call.session_factory is None:
        yield
        return
    async with hold_live_call(call.project_id, call.call_id):
        yield


async def _build(
    call: CallToRun, make_transport: TransportFactory, settings: Settings
) -> CallSession:
    """The call's session, built. MCP is closed on every path where the
    session will not close it: a build that raises or is cancelled."""
    config = call.agent.config
    mcp_manager = _mcp_manager(call)
    session: CallSession | None = None
    try:
        mcp_tools: list[Any] = []
        if mcp_manager is not None:
            mcp_tools = await mcp_manager.connect_servers(config.mcp_servers)
        mixer = await build_ambience_mixer(
            config,
            project_id=call.project_id,
            sample_rate=make_transport.sample_rate,
            session_factory=call.session_factory,
            settings=settings,
        )
        session = await build_call_pipeline(
            agent=call.agent,
            transport=make_transport.make(mixer=mixer),
            call_context=_context(call, mcp_manager),
            settings=settings,
            session_factory=call.session_factory,
            audio_sample_rate=make_transport.sample_rate,
            mcp_tools=mcp_tools or None,
            carries_video=make_transport.carries_video,
        )
    except asyncio.CancelledError:
        # No session exists to end the call, and the carrier's own ending
        # cannot end one still at `initiated`: ended here, as a hangup during
        # setup ends — completed, never `pipeline_error`. Shielded, since it
        # runs while the task is being cancelled.
        await asyncio.shield(_end_unstarted(call))
        raise
    finally:
        # `except Exception` alone skipped this when the build was cancelled.
        if session is None and mcp_manager is not None:
            await mcp_manager.close()
    return session


async def _end_unstarted(call: CallToRun) -> None:
    """A call cancelled before its pipeline existed: ended, and `call.ended`
    sent. Nothing with no database (the demo). Never raises: the caller is
    re-raising the cancellation."""
    if call.session_factory is None:
        return
    try:
        await finalize_call(call.session_factory, call.call_id, failed=False)
    except Exception:
        logger.exception("call_end_unstarted_error", call_id=str(call.call_id))


def _mcp_manager(call: CallToRun) -> Any | None:
    """The agent's MCP servers, not yet connected; None without any."""
    if not call.agent.config.mcp_servers:
        return None
    from turncall.services.mcp_client import MCPSessionManager

    return MCPSessionManager(call_id=call.call_id, project_id=call.project_id)


def _context(call: CallToRun, mcp_manager: Any | None) -> CallContext:
    return CallContext(
        call_id=call.call_id,
        project_id=call.project_id,
        # The sentinel, never written anywhere: an inline agent has no row.
        agent_id=call.agent_id or DYNAMIC_AGENT_ID,
        call_sid=call.call_sid,
        stream_sid=call.stream_sid,
        session_factory=call.session_factory,
        mcp_manager=mcp_manager,
    )
