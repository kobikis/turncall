"""Per-call session managing the Pipecat pipeline lifecycle."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from loguru import logger
from pipecat.frames.frames import ErrorFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import (
    PipelineParams,
    PipelineWorker,
    ProcessorUnusablePolicy,
)
from pipecat.utils.errors import ErrorCategory
from pipecat.workers.runner import WorkerRunner

from turncall.config import get_settings
from turncall.domain.enums import CallEventType, CallStatus
from turncall.orchestrator.pipeline_factory import CallContext
from turncall.orchestrator.telemetry import (
    build_observers,
    build_span_attributes,
    is_tracing_active,
)
from turncall.services.credentials import MissingProviderKeyError


def s2s_greeting_frame() -> LLMRunFrame:
    """The frame that opens an S2S session: a run of the pipeline's own context.

    The user aggregator answers an `LLMRunFrame` by pushing the context the
    pipeline was built with — tools included. A fresh `LLMContext` here used to
    start the session instead, and gpt-live-1 configures its session (its
    delegate's tools among it) from the first context it sees and never again,
    so an `openai_live` agent's delegate had no tools for the whole call (#243).
    """
    return LLMRunFrame()


async def run_until_pipeline_done(runner: WorkerRunner, worker: PipelineWorker) -> None:
    """Run `worker` under `runner` and return once its pipeline is done.

    A child worker does not end with its pipeline: an `openai_live` client
    delegate (#244) is one, and when the pipeline ends (the hangup gate's
    `EndWorkerFrame`) or is cancelled directly (a hangup, the duration or idle
    guard), pipecat 1.12's runner waited on it forever — the call was never
    finalized, `call.ended` never sent. Cancelling the runner once the
    pipeline is done ends the children too, and is a no-op otherwise.
    """
    await runner.add_workers(worker)
    run = asyncio.create_task(runner.run())
    finished = asyncio.create_task(worker.wait())
    try:
        await asyncio.wait({run, finished}, return_when=asyncio.FIRST_COMPLETED)
        await runner.cancel(reason="pipeline finished")
        await run  # re-raises what the runner raised
    finally:
        for task in (run, finished):
            task.cancel()
        await asyncio.gather(run, finished, return_exceptions=True)


class CallSession:
    """Manages the lifecycle of a single voice call's Pipecat pipeline.

    Each active call has one CallSession. The session owns the transport,
    pipeline, and bridges pipeline events to the call state machine.
    """

    def __init__(
        self,
        call_context: CallContext,
        transport: object,
        pipeline: Pipeline,
        *,
        first_message: str | None = None,
        pipeline_mode: str = "cascade",
        s2s_provider: str | None = None,
        max_call_duration_seconds: int | None = None,
        idle_message: str = "",
    ) -> None:
        self._call_context = call_context
        self._transport = transport
        self._pipeline = pipeline
        self._first_message = first_message
        self._pipeline_mode = pipeline_mode
        self._s2s_provider = s2s_provider
        self._max_call_duration_seconds = max_call_duration_seconds
        self._idle_message = idle_message
        self._idle_strikes = 0
        self._task: PipelineWorker | None = None
        self._runner: WorkerRunner | None = None
        self._duration_guard: asyncio.Task | None = None
        self._running = False
        # An exception that escaped into start(), kept rather than only logged.
        # start() swallows it on purpose — a live call must still finalize —
        # but an eval has to tell a platform fault from an agent result, which
        # is the difference between `errored` and `failed`. Note this is NOT
        # how a refused provider shows up: pipecat ends that pipeline
        # gracefully, and an eval is supposed to score it `failed`.
        self._failure: BaseException | None = None
        # Why the call failed, when something did. A provider pipecat stops
        # using (bad key, unknown model) ends the pipeline gracefully, so
        # without this a 401 on the first turn read as the caller hanging up.
        self._error: str | None = None
        # Every error the pipeline reported, ending or not. A provider that
        # refuses a request without being marked unusable (Bedrock's model
        # access check, say) leaves the pipeline running and silent; an eval
        # then fails on "no response" with nothing saying why.
        self.pipeline_errors: list[str] = []

    @property
    def call_id(self) -> UUID:
        return self._call_context.call_id

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def failure(self) -> BaseException | None:
        """The exception that ended the pipeline, if one did."""
        return self._failure

    async def _build_telemetry(self) -> tuple[list, dict]:
        """Observers + span attributes for this call's pipeline task (ADR-0010).

        Loads the call row once (at start, not on the frame path) for
        direction/numbers; degrades to IDs-only if that read fails.
        """
        pipecat = get_settings().pipecat
        observers = build_observers(pipecat.enable_observers)
        ctx = self._call_context
        direction = transport = from_number = to_number = None
        try:
            async with ctx.session_factory() as session:
                from turncall.storage.repositories import call_repo

                call = await call_repo.get_call_by_id(session, ctx.call_id)
            if call:
                direction = call.direction
                transport = call.provider
                from_number = call.from_number
                to_number = call.to_number
        except Exception:
            logger.warning("telemetry_call_load_failed", call_id=str(ctx.call_id))
        attrs = build_span_attributes(
            project_id=ctx.project_id,
            agent_id=ctx.agent_id,
            call_sid=ctx.call_sid,
            direction=direction,
            transport=transport,
            from_number=from_number,
            to_number=to_number,
            include_pii=pipecat.trace_include_pii,
        )
        return observers, attrs

    async def start(self) -> None:
        """Start the pipeline for this call."""
        logger.info(
            "call_session_starting",
            call_id=str(self.call_id),
            agent_id=str(self._call_context.agent_id),
        )

        self._runner = WorkerRunner()
        # Metrics on so TTS/processing timing is visible — needed to catch the
        # event-loop stalls behind transient mid-word audio cut-outs, and to feed
        # the trace spans' TTFB/token attributes.
        observers, span_attrs = await self._build_telemetry()
        # The other half of the eval RTVI bridge (evals-design §9.7). Appended
        # after the telemetry observers and outside PIPECAT_ENABLE_OBSERVERS:
        # this one is the protocol, not instrumentation, and the harness's
        # handshake configures it. Empty on every live call.
        from turncall.orchestrator.pipeline_factory import eval_rtvi_observers
        from turncall.orchestrator.turn_metrics import turn_metrics_observers

        # Turn metrics are product data (#176), not instrumentation, so they sit
        # outside PIPECAT_ENABLE_OBSERVERS like the RTVI bridge. None in an eval.
        observers = [
            *observers,
            *eval_rtvi_observers(self._pipeline),
            *turn_metrics_observers(self._call_context),
        ]
        self._task = PipelineWorker(
            self._pipeline,
            params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
            observers=observers,
            # Pipecat 1.8 stops using an STT/TTS/LLM that can no longer work (bad
            # key, unknown model/voice, dead socket) instead of retrying per chunk.
            # The default CONTINUE would leave the caller on a silent line until
            # the 300s idle timeout; END drains and stops, so _finalize_call runs
            # promptly and the call is finalized instead of hanging.
            processor_unusable_policy=ProcessorUnusablePolicy.END,
            enable_tracing=is_tracing_active(),
            enable_turn_tracking=True,
            conversation_id=str(self.call_id),
            additional_span_attributes=span_attrs,
        )
        self._running = True

        # A hangup (Twilio 'stop' + WS close, or a WebRTC peer disconnect) closes
        # the transport but pushes NO EndFrame into the pipeline, so the runner
        # would hang forever and the finalize in this method's `finally` block would
        # never run — leaving the call stuck at in_progress. Cancelling the task on
        # disconnect makes run() return so the call is finalized. Works for every
        # transport (all emit on_client_disconnected); Pipecat appends handlers, so
        # this coexists with the recorder's flush handler.
        @self._transport.event_handler("on_client_disconnected")  # type: ignore[attr-defined]
        async def _end_pipeline_on_disconnect(*_args: object) -> None:
            logger.info("client_disconnected", call_id=str(self.call_id))
            if self._task is not None:
                await self._task.cancel()

        @self._task.event_handler("on_pipeline_error")
        async def _record_provider_error(_worker: object, frame: ErrorFrame) -> None:
            await self._on_pipeline_error(frame)

        self._duration_guard = self._start_duration_guard()
        self._arm_idle_guard()
        self._watch_failover()

        try:
            await self._update_call_status(CallStatus.IN_PROGRESS)
            # Note: call.started event is fired by the inbound handler
            # (twilio_handlers / whatsapp_handlers) before the pipeline starts.

            await self._trigger_first_response(self._task)
            await run_until_pipeline_done(self._runner, self._task)
        except asyncio.CancelledError:
            logger.info("call_session_cancelled", call_id=str(self.call_id))
        except Exception as exc:
            logger.exception("call_session_error", call_id=str(self.call_id))
            self._failure = exc
            await self._record_error("pipeline", exc)
        finally:
            self._running = False
            # Before finalizing: a call that ended on its own must not have the
            # guard fire minutes later against a worker that is already gone.
            if self._duration_guard is not None:
                self._duration_guard.cancel()
                self._duration_guard = None
            try:
                await self._finalize_call()
            except Exception:
                logger.exception("call_cleanup_error", call_id=str(self.call_id))
            # Clean up MCP sessions
            if self._call_context.mcp_manager is not None:
                try:
                    await self._call_context.mcp_manager.close()
                except Exception:
                    logger.exception("mcp_cleanup_error", call_id=str(self.call_id))
            # Post-call analysis + call.ended are dispatched above on the
            # completing edge (also from the Twilio /status callback and the
            # end_call tool for their respective paths). Cancelling the task on
            # on_client_disconnected guarantees we reach here promptly instead of
            # hanging in the runner.
            logger.info("call_session_ended", call_id=str(self.call_id))

    async def _trigger_first_response(self, task: PipelineWorker) -> None:
        """Queue what makes the agent speak first."""
        if self._pipeline_mode == "s2s":
            if self._s2s_provider == "openai_live":
                # gpt-live-1 connects in setup(), before any frame is processed,
                # and drops the caller's audio until the session this frame
                # starts — so a wait here is the caller's first words lost (#256).
                await task.queue_frame(s2s_greeting_frame())
                return

            # Realtime, Gemini Live, Nova Sonic: a wait for the WebSocket to
            # connect, never measured without it.
            async def _trigger_s2s_greeting() -> None:
                await asyncio.sleep(2)
                logger.info("S2S: triggering initial greeting")
                await task.queue_frame(s2s_greeting_frame())

            self._greeting_task = asyncio.create_task(_trigger_s2s_greeting())
            return

        # Cascade: use TTSSpeakFrame for direct TTS playback
        from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame

        if self._first_message:
            await task.queue_frame(
                TTSSpeakFrame(text=self._first_message, append_to_context=False)
            )
        else:
            await task.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": "Start the conversation."}],
                    run_llm=True,
                )
            )

    async def _on_pipeline_error(self, frame: ErrorFrame) -> None:
        """Record an error that ends the pipeline, so the call is finalized as
        failed with the reason on its event log (ADR-0008)."""
        processor = frame.processor
        noted = f"{processor}: {readable_error(frame.error)}"
        if (
            noted not in self.pipeline_errors
            and len(self.pipeline_errors) < _MAX_PIPELINE_ERRORS
        ):
            self.pipeline_errors.append(noted)
        if not (frame.fatal or (processor is not None and not processor.is_usable)):
            return
        error = readable_error(frame.error)
        provider = provider_of(processor)
        if provider and frame.category in _KEY_REFUSED:
            # A key that is there but wrong (a missing one never gets this far).
            error = f"The {provider} API key was rejected: {error}"
            await self._record_error(
                str(processor), error, provider=provider, key_rejected=True
            )
            return
        await self._record_error(str(processor), error)

    def _watch_failover(self) -> None:
        """Record each LLM failover (#222): a `provider.failover` event on a
        call, and among the iteration's agent errors on an eval, which has no
        event log. The switch absorbs the error, so nothing else would."""
        from turncall.orchestrator.llm_failover import FailoverLLM

        processors = self._pipeline.processors
        # A client delegate's group (#246) sits outside the pipeline, behind
        # the live service.
        candidates = [
            *processors,
            *(getattr(p, "delegate_llm", None) for p in processors),
        ]
        group = next((p for p in candidates if isinstance(p, FailoverLLM)), None)
        if group is None:
            return

        @group.event_handler("on_failover")  # type: ignore[untyped-decorator]
        async def _on_failover(
            _group: object, src: str, dst: str, frame: ErrorFrame
        ) -> None:
            error = readable_error(frame.error)
            logger.warning(
                "provider_failover",
                call_id=str(self.call_id),
                role="llm",
                src=src,
                dst=dst,
                error=error,
            )
            if len(self.pipeline_errors) < _MAX_PIPELINE_ERRORS:
                self.pipeline_errors.append(
                    f"llm failed over from {src} to {dst}: {error}"
                )
            if not self._call_context.is_eval:
                await self._log_event(
                    CallEventType.PROVIDER_FAILOVER,
                    {"role": "llm", "from": src, "to": dst, "error": error},
                )

    def _arm_idle_guard(self) -> None:
        """React when the caller goes quiet after the agent has spoken.

        The timer itself is Pipecat's: `LLMUserAggregatorParams.user_idle_timeout`
        (set in the pipeline factory from `user_idle_timeout_ms`) starts on
        BotStoppedSpeaking and cancels when anyone speaks. It only tells us the
        caller has gone quiet; what to do about it is ours.

        Two strikes. The first speaks `idle_message`, the second ends the call —
        what a phone system is expected to do, and the point of the feature: a
        silent line otherwise bills until `max_call_duration_seconds` or the
        carrier gives up.

        The aggregator is found by walking the pipeline. Use `processors`, not
        `processors_with_metrics()`: the latter keeps only processors where
        `can_generate_metrics()` is true, which an aggregator is not, so it
        never returned one and the guard silently disarmed itself on every
        call. `processors` is a property holding every child, so recurse for
        the nested-pipeline case that `processors_with_metrics` handled itself.
        """
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMUserAggregator,
        )

        def _walk(processor: object) -> Iterator[object]:
            for child in getattr(processor, "processors", []):
                yield child
                yield from _walk(child)

        aggregator = next(
            (p for p in _walk(self._pipeline) if isinstance(p, LLMUserAggregator)),
            None,
        )
        if aggregator is None:
            logger.warning("idle_guard_no_aggregator", call_id=str(self.call_id))
            return

        @aggregator.event_handler("on_user_turn_started")  # type: ignore[misc]
        async def _reset_strikes(*_args: object) -> None:
            # Without this a caller who pauses once, speaks, then pauses again
            # is hung up on the second pause — the strikes have to count
            # consecutive silences, not silences per call.
            self._idle_strikes = 0

        @aggregator.event_handler("on_user_turn_idle")  # type: ignore[misc]
        async def _on_idle(*_args: object) -> None:
            live = self._live_service()
            if live is not None and live.delegating:
                # gpt-live-1 went quiet to wait for its delegate, and the
                # caller with it; pipecat holds the timer for a function call
                # in this pipeline only. The relayed answer re-arms it (#257).
                logger.info("user_idle_while_delegating", call_id=str(self.call_id))
                return
            self._idle_strikes += 1
            logger.info(
                "user_idle",
                call_id=str(self.call_id),
                strike=self._idle_strikes,
            )
            if self._idle_strikes == 1:
                await self._speak_idle_prompt()
                return
            await self._end_for_silence()

    def _live_service(self) -> Any:
        """gpt-live-1's service on an `openai_live` call, else None."""
        if self._pipeline_mode != "s2s" or self._s2s_provider != "openai_live":
            return None
        from turncall.orchestrator.delegation_events import (
            DelegationTrackingLiveService,
        )

        return next(
            (
                p
                for p in self._pipeline.processors
                if isinstance(p, DelegationTrackingLiveService)
            ),
            None,
        )

    async def _speak_idle_prompt(self) -> None:
        """Ask if the caller is still there.

        Cascade speaks the configured line through TTS. S2S has no TTS stage —
        none of the realtime services handle a TTSSpeakFrame — so the model is
        asked to check in and says it in its own words, the same split
        `start()` already makes for the first message — gpt-live-1 as spoken
        commentary, since it reads no context after its first (#257).
        """
        if self._task is None:
            return

        live = self._live_service()
        if live is not None:
            await live.say(_S2S_CHECK_IN)
            return

        if self._pipeline_mode == "s2s":
            from pipecat.frames.frames import LLMMessagesAppendFrame

            await self._task.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": _S2S_CHECK_IN}],
                    run_llm=True,
                )
            )
            return

        from pipecat.frames.frames import TTSSpeakFrame

        await self._task.queue_frame(
            TTSSpeakFrame(text=self._idle_message, append_to_context=False)
        )

    async def _end_for_silence(self) -> None:
        """Give up on a caller who never came back.

        Recorded before the hangup for the same reason as the duration cap:
        `_finalize_call` derives ended_reason from the event log, and without
        this event the call reads as `assistant_ended_call` — true of the
        mechanism, wrong about what happened. See ADR-0008.
        """
        logger.info("call_customer_silent", call_id=str(self.call_id))
        await self._log_event(
            CallEventType.CALL_CUSTOMER_SILENT,
            {"strikes": self._idle_strikes},
        )
        if self._task is not None:
            await self._task.cancel()

    def _start_duration_guard(self) -> asyncio.Task | None:
        """Hang up when the call outlives `max_call_duration_seconds`.

        The field was accepted and validated 60-14400 and read by nothing, so
        nothing ever ended a call at the configured duration: a stuck one ran
        until the carrier or an idle timeout stopped it, billed the whole way.

        Cancelling the worker is how a hangup already ends a call here — see
        the disconnect handler above — so run() returns and the `finally` in
        start() finalizes as usual.
        """
        budget = self._max_call_duration_seconds
        if not budget:
            return None

        async def _guard() -> None:
            await asyncio.sleep(budget)
            logger.warning(
                "call_max_duration_reached",
                call_id=str(self.call_id),
                seconds=budget,
            )
            # Recorded before the hangup: _finalize_call reads the event log to
            # derive ended_reason, and this is the only trace of why.
            await self._log_event(
                CallEventType.CALL_MAX_DURATION_REACHED,
                {"max_call_duration_seconds": budget},
            )
            if self._task is not None:
                await self._task.cancel()

        return asyncio.create_task(_guard())

    async def _finalize_call(self) -> None:
        """Finalize the call on the completing edge of the pipeline.

        Transport-agnostic: this is the only place that reliably runs for a
        caller-initiated hangup (inbound Twilio never gets a /status callback;
        WebRTC/WhatsApp have none at all). Failed when an error ended the
        pipeline, completed otherwise.

        An eval has no call to finalize, and must not dispatch `call.ended` to
        subscribers for a conversation that was never a call.
        """
        if self._call_context.is_eval:
            return
        await finalize_call(
            self._call_context.session_factory,
            self._call_context.call_id,
            failed=self._error is not None,
        )

    async def stop(self) -> None:
        """Gracefully stop the pipeline."""
        if self._task is not None:
            await self._task.cancel()
        self._running = False

    async def _update_call_status(self, status: CallStatus) -> None:
        """Bridge pipeline lifecycle to call state machine."""
        if self._call_context.is_eval:
            # An eval iteration has no `calls` row; the run row is its record.
            return
        try:
            async with self._call_context.session_factory() as session:
                from turncall.storage.repositories import call_repo

                # Stamp started_at from the pipeline itself rather than relying on
                # the Twilio status callback (which may not be configured, can fail
                # signature, and doesn't exist for WebRTC/WhatsApp).
                started_at = (
                    datetime.now(UTC) if status == CallStatus.IN_PROGRESS else None
                )
                await call_repo.update_call_status(
                    session,
                    self._call_context.call_id,
                    status=status.value,
                    started_at=started_at,
                )
                await session.commit()
        except Exception:
            logger.exception(
                "call_status_update_error",
                call_id=str(self.call_id),
                status=status.value,
            )

    async def _log_event(self, event_type: CallEventType, payload: dict) -> None:
        """Log a call event to the database and dispatch webhook. None in an
        eval, which has no call row to hang it off (ADR-0018)."""
        if self._call_context.is_eval:
            return
        await log_call_event(self._call_context, event_type, payload)

    async def _record_error(self, stage: str, error: object, **detail: Any) -> None:
        """Keep the first error that ends the call, on its event log too."""
        if self._error is not None or self._call_context.is_eval:
            return
        self._error = readable_error(error)
        await self._log_event(
            CallEventType.ERROR_RAISED,
            {"stage": stage, "error": self._error, **detail},
        )


# Provider SDKs put the whole request in their message (Deepgram: every header,
# the token redacted), which buries the one sentence a person needs.
_REQUEST_HEADERS = re.compile(r"headers: \{.*?\}, ")


# Enough to say why an iteration went wrong without a runaway provider
# filling the eval's results entry.
_MAX_PIPELINE_ERRORS = 5

_KEY_REFUSED = (ErrorCategory.AUTHENTICATION, ErrorCategory.AUTHORIZATION)

# What an S2S model is asked on the first idle strike; it words the check-in.
_S2S_CHECK_IN = "The caller has gone quiet. Briefly check whether they are still there."


def provider_of(processor: object) -> str | None:
    """The vendor a pipecat service belongs to, read off its module
    (`pipecat.services.deepgram.stt` -> `deepgram`); None for anything else."""
    parts = type(processor).__module__.split(".")
    return parts[2] if parts[:2] == ["pipecat", "services"] and len(parts) > 2 else None


def readable_error(error: object) -> str:
    """An error as the call's event log keeps it: the reason, not the request."""
    return _REQUEST_HEADERS.sub("", str(error))[:500]


async def log_call_event(
    call_context: CallContext, event_type: CallEventType, payload: dict
) -> None:
    """Log a call event to the database and dispatch webhook. Never raises."""
    try:
        async with call_context.session_factory() as session:
            from turncall.events.emit import emit_call_event

            await emit_call_event(
                session,
                call_id=call_context.call_id,
                project_id=call_context.project_id,
                event_type=event_type,
                payload=payload,
            )
            await session.commit()
    except Exception:
        logger.exception("call_event_log_error", call_id=str(call_context.call_id))


async def finalize_call(session_factory: Any, call_id: UUID, *, failed: bool) -> None:
    """Set the terminal status, ended_at and duration, then dispatch
    `call.ended` + post-call analysis.

    The `status not in (completed, failed)` guard keeps this idempotent vs the
    /status callback and the end_call tool — whichever finalizes first wins.
    """
    from turncall.services.call_analysis_trigger import (
        config_for_call,
        trigger_post_call_analysis,
    )
    from turncall.storage.repositories import call_repo

    async with session_factory() as session:
        call = await call_repo.get_call_by_id(session, call_id)
        if not call or call.status in ("completed", "failed"):
            return

        ended_at = datetime.now(UTC)
        duration_ms = None
        if call.started_at is not None:
            duration_ms = int((ended_at - call.started_at).total_seconds() * 1000)
        status = CallStatus.FAILED if failed else CallStatus.COMPLETED
        await call_repo.update_call_status(
            session,
            call_id,
            status=status.value,
            ended_at=ended_at,
            duration_ms=duration_ms,
        )
        await session.commit()

        # Not gated on active_agent_id: a call running an inline agent has
        # none, and this is what dispatches call.ended.
        config = await config_for_call(session, call)
        if config is not None:
            trigger_post_call_analysis(
                session_factory, call.id, call.project_id, config
            )


async def fail_unstarted_call(call_context: CallContext, error: BaseException) -> None:
    """A call whose pipeline never ran: failed, with why, and `call.ended`.

    No CallSession exists to finalize it, so without this the row sat at its
    initial status and subscribers heard nothing. Never raises — the caller is
    already handling the original error.
    """
    if call_context.is_eval:
        return
    try:
        await log_call_event(
            call_context,
            CallEventType.ERROR_RAISED,
            {
                "stage": "pipeline_build",
                "error": readable_error(error),
                **(
                    {"missing_keys": error.accounts}
                    if isinstance(error, MissingProviderKeyError)
                    else {}
                ),
            },
        )
        await finalize_call(
            call_context.session_factory, call_context.call_id, failed=True
        )
    except Exception:
        logger.exception("call_fail_record_error", call_id=str(call_context.call_id))
