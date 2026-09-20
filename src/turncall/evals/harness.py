"""The bridge: the real pipeline on one end of a loopback socket, pipecat's
harness on the other (ADR-0018).

Pipecat's eval harness is an RTVI WebSocket *client* and the bot hosts the
server, so a run is both halves in one process. Going around the socket would
mean reimplementing the matcher, the judge, the persona driver, the latency
budgets and the event stream; the socket is the cheap price for all of it.

The property that makes this worth building: only the transport is swapped. An
eval therefore runs the real `_create_stt_service` (with its per-provider
keyterm mapping), the real `_create_llm_service` (with the
Anthropic-no-temperature rule), the real VAD and smart-turn wiring, the real
tool bridge and KB retrieval — which is where #63, #64, #65 and #67 all lived.

A fresh pipeline per iteration, deliberately. `EvalSessionParams.stop_bot`
defaults to False and the transport is built to serve several scenarios in a
row, which would be faster — but a scripted scenario resets context via its
`context:` field while a simulation has no equivalent, so cross-scenario
leakage is a live hazard. Isolation first; optimise when someone measures it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any
from uuid import UUID, uuid4

from loguru import logger

from turncall.domain.enums import EvalKind, EvalModality
from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID, CallContext
from turncall.orchestrator.transport_factory import EVAL_SAMPLE_RATE

# How long to wait for the bot pipeline to wind down once the harness is done
# before giving up on it. The harness has already asked it to cancel.
_BOT_STOP_TIMEOUT_S = 15.0


class PipelineFailed(RuntimeError):
    """The pipeline raised, so the iteration proves nothing about the agent.

    Narrow on purpose. This is *our* fault — an exception escaping into
    `CallSession.start()` — not the agent's configuration being wrong.

    A provider rejecting at connect (a model name that does not exist, a key
    that is refused) must NOT come through here. Pipecat's
    `ProcessorUnusablePolicy.END` ends such a pipeline gracefully rather than
    raising, the harness times out with nothing to match, and the run scores
    `failed`. That is exactly right and is the feature's headline use case:
    evals-design.md section 1 justifies the whole thing on #63, #64 and #65,
    all of which are provider-rejects-at-connect. Scoring those `errored` would
    render them grey as "couldn't run", keep them out of every rate, and let
    the regression through — the opposite of what this is for.
    """


def _free_port() -> int:
    """A port nothing is listening on, for this run's loopback socket.

    ponytail: bind-then-close races anything else that grabs the port in the
    gap. The window is microseconds and a lost race fails one iteration as
    `errored`; swap for a per-worker port range if that ever shows up in
    practice.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _build_session(parsed: Any, kind: EvalKind, bot_url: str) -> Any:
    """The pipecat harness session for this scenario kind."""
    from pipecat.evals.session import EvalSessionParams

    params = EvalSessionParams(
        # A fresh pipeline per iteration means we do want the bot torn down,
        # and its `on_client_disconnected` fired, when the scenario ends.
        stop_bot=True,
        trigger_disconnect=True,
    )
    if kind is EvalKind.SCRIPTED:
        from pipecat.evals.script_session import EvalScriptSession

        return EvalScriptSession.from_scenario(parsed, bot_url, params=params)

    from pipecat.evals.simulation_session import EvalSimulationSession

    return EvalSimulationSession.from_scenario(parsed, bot_url, params=params)


async def run_iteration(
    *,
    parsed: Any,
    kind: EvalKind,
    target: Any,
    modality: EvalModality,
    settings: Any,
    session_factory: Any,
    run_id: UUID,
    tool_mocks: Any = None,
) -> Any:
    """Run one scenario-iteration end to end and return pipecat's result.

    Builds the agent's real pipeline against an eval transport, starts it, and
    drives it with pipecat's harness over loopback. Always tears the bot down,
    including when the harness raises — a leaked pipeline holds provider
    sockets and its port for the life of the worker.
    """
    from turncall.orchestrator.pipeline_builder import build_call_pipeline
    from turncall.orchestrator.transport_factory import create_eval_transport

    audio = modality is EvalModality.AUDIO
    port = _free_port()
    transport = create_eval_transport(port, audio=audio)

    call_context = CallContext(
        # An eval has no call. The id still has to be unique — it keys the
        # pipeline's own bookkeeping (traces, conversation id) — but nothing
        # reads it back out of a `calls` row, because there is none.
        call_id=uuid4(),
        project_id=target.project_id,
        # ADR-0017's sentinel for a target with no agent row behind it — never
        # a locally invented one, and never written to a column.
        agent_id=target.agent_id or DYNAMIC_AGENT_ID,
        call_sid=f"eval-{run_id}",
        stream_sid=f"eval-{run_id}",
        session_factory=session_factory,
        eval_run_id=run_id,
        # The scenario's mocks + policy, short-circuited in the tool bridge
        # before anything is dispatched (#71).
        tool_mocks=tool_mocks,
    )

    session = await build_call_pipeline(
        config=target.config,
        transport=transport,
        call_context=call_context,
        settings=settings,
        session_factory=session_factory,
        audio_sample_rate=EVAL_SAMPLE_RATE,
    )

    bot = asyncio.create_task(session.start(), name=f"eval-bot-{run_id}")
    try:
        harness = _build_session(parsed, kind, f"ws://127.0.0.1:{port}")
        result = await harness.run()
    finally:
        await _stop_bot(bot, run_id)

    # A pipeline that *raised* is a platform fault, so the iteration proves
    # nothing and is errored. A pipeline that a provider refused is not: see
    # PipelineFailed's docstring for why that has to stay `failed`. Checked
    # after the result so a run that completed anyway is scored on its own
    # terms.
    if session.failure is not None and not getattr(result, "passed", False):
        raise PipelineFailed(
            f"the agent pipeline stopped: {type(session.failure).__name__}: "
            f"{session.failure}"
        ) from session.failure
    return result


async def _stop_bot(bot: asyncio.Task, run_id: UUID) -> None:
    """Wind the bot pipeline down, cancelling it if it will not go quietly."""
    if bot.done():
        # CallSession.start() swallows its own exception (a live call must
        # finalize rather than propagate), so this almost never raises — the
        # pipeline's failure is read off `session.failure` instead. Kept for
        # anything that escapes start() entirely.
        try:
            bot.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("eval_bot_exited_early", run_id=str(run_id))
        return
    try:
        await asyncio.wait_for(asyncio.shield(bot), timeout=_BOT_STOP_TIMEOUT_S)
    except TimeoutError:
        logger.warning("eval_bot_stop_timeout", run_id=str(run_id))
        bot.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await bot
    except Exception:
        logger.exception("eval_bot_error", run_id=str(run_id))
