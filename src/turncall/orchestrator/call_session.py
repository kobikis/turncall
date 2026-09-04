"""Per-call session managing the Pipecat pipeline lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.pipeline.worker import ProcessorUnusablePolicy

from turncall.config import get_settings
from turncall.domain.enums import CallEventType, CallStatus
from turncall.orchestrator.pipeline_factory import CallContext
from turncall.orchestrator.telemetry import (
    build_observers,
    build_span_attributes,
    is_tracing_active,
)


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
    ) -> None:
        self._call_context = call_context
        self._transport = transport
        self._pipeline = pipeline
        self._first_message = first_message
        self._pipeline_mode = pipeline_mode
        self._task: PipelineTask | None = None
        self._runner: PipelineRunner | None = None
        self._running = False

    @property
    def call_id(self) -> UUID:
        return self._call_context.call_id

    @property
    def is_running(self) -> bool:
        return self._running

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

        self._runner = PipelineRunner()
        # Metrics on so TTS/processing timing is visible — needed to catch the
        # event-loop stalls behind transient mid-word audio cut-outs, and to feed
        # the trace spans' TTFB/token attributes.
        observers, span_attrs = await self._build_telemetry()
        self._task = PipelineTask(
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

        try:
            await self._update_call_status(CallStatus.IN_PROGRESS)
            # Note: call.started event is fired by the inbound handler
            # (twilio_handlers / whatsapp_handlers) before the pipeline starts.

            # Trigger first agent response
            if self._pipeline_mode == "s2s":
                # S2S: the Realtime WebSocket takes time to connect. Queue a
                # delayed context frame to trigger the greeting after connection.
                from pipecat.frames.frames import LLMContextFrame
                from pipecat.processors.aggregators.llm_context import (
                    LLMContext as PipecatLLMContext,
                )

                async def _trigger_s2s_greeting() -> None:
                    await asyncio.sleep(2)
                    logger.info("S2S: triggering initial greeting")
                    ctx = PipecatLLMContext(messages=[])
                    await self._task.queue_frame(LLMContextFrame(context=ctx))

                self._greeting_task = asyncio.create_task(_trigger_s2s_greeting())
            else:
                # Cascade: use TTSSpeakFrame for direct TTS playback
                from pipecat.frames.frames import (
                    LLMMessagesAppendFrame,
                    TTSSpeakFrame,
                )

                if self._first_message:
                    await self._task.queue_frame(
                        TTSSpeakFrame(text=self._first_message, append_to_context=False)
                    )
                else:
                    await self._task.queue_frame(
                        LLMMessagesAppendFrame(
                            messages=[
                                {"role": "user", "content": "Start the conversation."}
                            ],
                            run_llm=True,
                        )
                    )

            await self._runner.run(self._task)
        except asyncio.CancelledError:
            logger.info("call_session_cancelled", call_id=str(self.call_id))
        except Exception:
            logger.exception("call_session_error", call_id=str(self.call_id))
            await self._update_call_status(CallStatus.FAILED)
        finally:
            self._running = False
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

    async def _finalize_call(self) -> None:
        """Finalize the call on the completing edge of the pipeline.

        Transport-agnostic: this is the only place that reliably runs for a
        caller-initiated hangup (inbound Twilio never gets a /status callback;
        WebRTC/WhatsApp have none at all). Sets COMPLETED + ended_at + pipeline-
        authoritative duration, then dispatches call.ended + post-call analysis.
        The `status not in (completed, failed)` guard keeps this idempotent vs the
        /status callback and the end_call tool — whichever finalizes first wins.
        """
        async with self._call_context.session_factory() as session:
            from turncall.storage.repositories import call_repo

            call = await call_repo.get_call_by_id(
                session, self._call_context.call_id
            )
            if not call or call.status in ("completed", "failed"):
                return

            ended_at = datetime.now(UTC)
            duration_ms = None
            if call.started_at is not None:
                duration_ms = int(
                    (ended_at - call.started_at).total_seconds() * 1000
                )
            await call_repo.update_call_status(
                session,
                self._call_context.call_id,
                status=CallStatus.COMPLETED.value,
                ended_at=ended_at,
                duration_ms=duration_ms,
            )
            await session.commit()

            if call.active_agent_id:
                from turncall.storage.repositories import agent_repo

                agent = await agent_repo.get_agent_by_id(
                    session, call.active_agent_id
                )
                if agent is not None:
                    from turncall.services.call_analysis_trigger import (
                        trigger_post_call_analysis,
                    )

                    trigger_post_call_analysis(
                        self._call_context.session_factory,
                        call.id,
                        call.project_id,
                        agent.config_blob,
                    )

    async def stop(self) -> None:
        """Gracefully stop the pipeline."""
        if self._task is not None:
            await self._task.cancel()
        self._running = False

    async def _update_call_status(self, status: CallStatus) -> None:
        """Bridge pipeline lifecycle to call state machine."""
        try:
            async with self._call_context.session_factory() as session:
                from turncall.storage.repositories import call_repo

                # Stamp started_at from the pipeline itself rather than relying on
                # the Twilio status callback (which may not be configured, can fail
                # signature, and doesn't exist for WebRTC/WhatsApp).
                started_at = (
                    datetime.now(UTC)
                    if status == CallStatus.IN_PROGRESS
                    else None
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
        """Log a call event to the database and dispatch webhook."""
        try:
            async with self._call_context.session_factory() as session:
                from turncall.events.emit import emit_call_event

                await emit_call_event(
                    session,
                    call_id=self._call_context.call_id,
                    project_id=self._call_context.project_id,
                    event_type=event_type,
                    payload=payload,
                )
                await session.commit()
        except Exception:
            logger.exception(
                "call_event_log_error",
                call_id=str(self.call_id),
            )
