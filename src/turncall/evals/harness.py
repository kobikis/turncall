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

A fresh pipeline per iteration, deliberately: a scripted scenario resets
context via its `context:` field while a simulation has no equivalent, so
cross-scenario leakage is a live hazard. Since pipecat 1.13 the harness ends
the bot's session after every run anyway (`eval-cancel`, pipecat#6064), and
`EvalSessionParams.stop_bot` is a deprecated no-op (#375).

The bot's end of the socket is `bot_server.LoopbackBot`: the pipeline is built
once the harness has connected, because the transport is built from that
connection.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import UUID, uuid4

from loguru import logger

from turncall.domain.enums import EvalKind, EvalModality
from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID, CallContext
from turncall.orchestrator.transport_factory import EVAL_SAMPLE_RATE

# How long to wait for the bot pipeline to wind down once the harness is done
# before giving up on it. The harness has already asked it to cancel.
_BOT_STOP_TIMEOUT_S = 15.0


class IterationTimeout(RuntimeError):
    """The harness did not finish inside this iteration's budget (#93).

    `errored`, like every other harness fault: an iteration that did not
    complete says nothing about the agent, and must stay out of the pass/fail
    rates for the same reason `PipelineFailed` does.
    """


class PipelineFailed(RuntimeError):
    """The pipeline raised, so the iteration proves nothing about the agent.

    Narrow on purpose. This is *our* fault — an exception escaping into
    `CallSession.start()` — not the agent's configuration being wrong.

    A provider rejecting at connect (a model name that does not exist, a key
    that is refused) must NOT come through here. Pipecat's
    `ProcessorUnusablePolicy.END` ends such a pipeline gracefully rather than
    raising, the harness times out with nothing to match, and the run scores
    `failed`. That is exactly right and is the feature's headline use case:
    evals exist because of #63, #64 and #65, all of which are
    provider-rejects-at-connect (ADR-0018). Scoring those `errored` would
    render them grey as "couldn't run", keep them out of every rate, and let
    the regression through — the opposite of what this is for.
    """


def _tts_cache_dir(settings: Any) -> str | None:
    """The caller-audio cache directory, created if it is not there yet.

    Pipecat writes into it lazily; making it here means a misconfigured path
    fails in the worker's log rather than halfway through synthesizing a turn.
    """
    import os

    configured = getattr(getattr(settings, "evals", None), "tts_cache_dir", None)
    if not configured:
        return None
    os.makedirs(configured, exist_ok=True)
    return str(configured)


def _build_session(
    parsed: Any, kind: EvalKind, bot_url: str, *, tts_cache_dir: str | None = None
) -> Any:
    """The pipecat harness session for this scenario kind."""
    from pipecat.evals.session import EvalSessionParams

    params = EvalSessionParams(
        # No `stop_bot`/`trigger_disconnect`: deprecated in 1.13 (#375), and
        # the harness now always ends the bot's session when a run ends.
        # Where the caller's synthesized turns are kept between runs (#72).
        # Same scenario, same voice, same text: synthesized once. Without a
        # path it lands under $HOME, which a container loses on recreate.
        cache_dir=tts_cache_dir,
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
    tool_mocks: Any,
    timeout_s: float | None = None,
    agent_errors: list[str] | None = None,
) -> Any:
    """Run one scenario-iteration end to end and return pipecat's result.

    `agent_errors`, when given, receives every error the agent's pipeline
    reported, whatever the outcome — the reason behind a reply that never came.

    Serves the harness a loopback socket, builds the agent's real pipeline on
    the connection it opens, and lets the harness drive it. Always tears the
    bot down, including when the harness raises — a leaked pipeline holds
    provider sockets and its port for the life of the worker.
    """
    from turncall.evals.bot_server import LoopbackBot
    from turncall.orchestrator.pipeline_builder import build_call_pipeline
    from turncall.orchestrator.transport_factory import create_eval_transport

    audio = modality is EvalModality.AUDIO
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
        # before anything is dispatched (#71). Required, not defaulted: a
        # caller that forgot it would run the agent's real tools, which is the
        # one thing this slice exists to prevent.
        tool_mocks=tool_mocks,
    )

    async def build(websocket: Any, flags: Any) -> Any:
        return await build_call_pipeline(
            config=target.config,
            transport=create_eval_transport(websocket, flags, audio=audio),
            call_context=call_context,
            settings=settings,
            session_factory=session_factory,
            audio_sample_rate=EVAL_SAMPLE_RATE,
        )

    bot = LoopbackBot(build, run_id)
    url = await bot.start()
    try:
        harness = _build_session(
            parsed, kind, url, tts_cache_dir=_tts_cache_dir(settings)
        )
        try:
            result = await _run_harness(harness, timeout_s, run_id)
        except Exception:
            # A pipeline that never got built leaves the harness with a closed
            # socket; the build's own exception is the one worth reporting.
            bot.raise_build_error()
            raise
        bot.raise_build_error()
    finally:
        if bot.pipeline is not None:
            await _stop_bot(bot.pipeline, run_id)
        await bot.close()
        if agent_errors is not None and bot.session is not None:
            agent_errors.extend(bot.session.pipeline_errors)

    session = bot.session
    # A pipeline that *raised* is a platform fault, so the iteration proves
    # nothing and is errored. A pipeline that a provider refused is not: see
    # PipelineFailed's docstring for why that has to stay `failed`. Checked
    # after the result so a run that completed anyway is scored on its own
    # terms.
    if (
        session is not None
        and session.failure is not None
        and not getattr(result, "passed", False)
    ):
        raise PipelineFailed(
            f"the agent pipeline stopped: {type(session.failure).__name__}: "
            f"{session.failure}"
        ) from session.failure
    return result


async def _run_harness(harness: Any, timeout_s: float | None, run_id: UUID) -> Any:
    """Drive the harness, inside a time budget (#93).

    Every service the harness itself drives — the persona LLM, its TTS, its
    STT, the judge — is a network call, and a provider that accepts a
    connection and then never answers is the ordinary failure, not an exotic
    one. Unbudgeted, that await never returns: `execute_run` never returns,
    the worker's concurrency slot is never released, and four of them leave a
    process that consumes no queue, executes nothing, logs nothing and still
    answers as healthy.

    The bot pipeline is wound down by the caller's `finally` either way.
    """
    if not timeout_s:
        return await harness.run()
    try:
        return await asyncio.wait_for(harness.run(), timeout=timeout_s)
    except TimeoutError as exc:
        logger.warning(
            "eval_iteration_timeout", run_id=str(run_id), budget_s=round(timeout_s, 1)
        )
        raise IterationTimeout(
            f"the harness did not finish within {timeout_s:.0f}s"
        ) from exc


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
