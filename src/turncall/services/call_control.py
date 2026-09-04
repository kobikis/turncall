"""Shared call control primitives.

Used by both built-in tools (via Pipecat function calling)
and live call control API endpoints. Always creates a TwilioAdapter
from env vars — no need to pass one as a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.call_state import is_active_state, is_valid_transition
from turncall.domain.enums import CallEventType, CallStatus


@dataclass(frozen=True)
class ControlResult:
    """Result of a call control action."""

    success: bool
    message: str
    details: dict[str, Any] | None = None


def _get_twilio_adapter() -> Any | None:
    """Create a TwilioAdapter from env var credentials. Returns None if not configured."""
    from turncall.config import get_settings

    settings = get_settings()
    if not settings.twilio.account_sid or not settings.twilio.auth_token:
        return None

    from turncall.adapters.telephony.twilio_adapter import TwilioAdapter

    return TwilioAdapter(settings.twilio.account_sid, settings.twilio.auth_token)


async def end_call(
    session: AsyncSession,
    call_id: UUID,
    *,
    reason: str = "normal",
) -> ControlResult:
    """End an active call via Twilio and update internal state."""
    from turncall.storage.repositories import call_repo

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None:
        return ControlResult(success=False, message="Call not found")

    current = CallStatus(call.status)
    if not is_active_state(current):
        return ControlResult(
            success=False,
            message=f"Call is not active (status: {current.value})",
        )

    if not is_valid_transition(current, CallStatus.COMPLETED):
        return ControlResult(
            success=False,
            message=f"Cannot end call from {current.value}",
        )

    # Hang up via Twilio
    sid = call.provider_call_sid
    if sid and sid != "webrtc":
        adapter = _get_twilio_adapter()
        if adapter:
            try:
                await adapter.end_call(sid)
            except Exception:
                logger.exception("twilio_hangup_failed", call_sid=sid)

    from datetime import UTC, datetime

    # This path often finalizes before the pipeline's _finalize_call (whose
    # idempotency guard then skips) — so it must compute duration itself.
    ended_at = datetime.now(UTC)
    duration_ms = None
    if call.started_at is not None:
        duration_ms = int((ended_at - call.started_at).total_seconds() * 1000)
    await call_repo.update_call_status(
        session,
        call_id,
        status=CallStatus.COMPLETED.value,
        ended_at=ended_at,
        duration_ms=duration_ms,
    )

    event_payload = {"reason": reason, "source": "control"}
    seq = await call_repo.get_next_sequence_number(session, call_id)
    await call_repo.create_call_event(
        session,
        call_id=call_id,
        event_type=CallEventType.CALL_ENDED,
        payload=event_payload,
        sequence_number=seq,
    )

    # Trigger post-call processing (transcript, recording, analysis, webhook)
    # The comprehensive call.ended webhook is dispatched after analysis completes.
    if call.active_agent_id:
        try:
            from turncall.storage.repositories import agent_repo

            agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)
            if agent:
                from turncall.services.call_analysis_trigger import (
                    trigger_post_call_analysis,
                )
                from turncall.storage.database import get_session_factory

                trigger_post_call_analysis(
                    get_session_factory(),
                    call_id,
                    call.project_id,
                    agent.config_blob,
                )
        except Exception:
            logger.exception("analysis_trigger_error", call_id=str(call_id))

    return ControlResult(success=True, message="Call ended")


async def transfer_call(
    session: AsyncSession,
    call_id: UUID,
    *,
    target_number: str,
    transfer_mode: str = "cold",
    transfer_message: str | None = None,
    briefing: object = None,
    fallback_message: str | None = None,
    reason: str | None = None,
) -> ControlResult:
    """Transfer an active call to a human, cold or warm. See ADR-0009."""
    from turncall.config import get_settings
    from turncall.services import transfer as transfer_svc
    from turncall.storage.repositories import call_repo

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None:
        return ControlResult(success=False, message="Call not found")

    current = CallStatus(call.status)
    if current != CallStatus.IN_PROGRESS:
        return ControlResult(
            success=False,
            message=f"Can only transfer from in_progress (current: {current.value})",
        )

    warm = transfer_mode == "warm"
    briefing_text, briefing_from_summary = transfer_svc.normalize_briefing(briefing)
    base_url = get_settings().server.public_base_url

    # Warm transfer and the no-answer fallback both need Twilio to call us back,
    # which needs a public URL. The caller transfer_message is inline and works
    # without one. Fail clearly rather than silently dropping features.
    if (warm or fallback_message) and not base_url:
        return ControlResult(
            success=False,
            message="PUBLIC_BASE_URL must be configured for warm transfer or a "
            "fallback message",
        )

    await call_repo.update_call_status(
        session, call_id, status=CallStatus.TRANSFERRING.value
    )

    whisper_url = action_url = amd_url = None
    if base_url:
        urls = transfer_svc.transfer_callback_urls(base_url, call_id)
        whisper_url = urls["whisper"] if warm else None
        action_url = urls["result"] if fallback_message else None
        amd_url = urls["amd"]  # AMD notify on both modes
        await transfer_svc.store_transfer_intent(
            call_id,
            transfer_svc.TransferIntent(
                target_number=target_number,
                transfer_mode=transfer_mode,
                transfer_message=transfer_message,
                briefing_text=briefing_text,
                briefing_from_summary=briefing_from_summary,
                fallback_message=fallback_message,
            ),
        )

    # Execute Twilio transfer
    sid = call.provider_call_sid
    transfer_success = False
    if sid and sid != "webrtc":
        adapter = _get_twilio_adapter()
        if adapter:
            try:
                await adapter.transfer_call(
                    sid,
                    target_number,
                    transfer_message=transfer_message,
                    whisper_url=whisper_url,
                    action_url=action_url,
                    amd_callback_url=amd_url,
                )
                transfer_success = True
            except Exception:
                logger.exception(
                    "twilio_transfer_failed", call_sid=sid, target=target_number
                )

    from turncall.events.emit import emit_call_event

    await emit_call_event(
        session,
        call_id=call_id,
        project_id=call.project_id,
        event_type=CallEventType.CALL_TRANSFERRED,
        payload={
            "target_number": target_number,
            "transfer_mode": transfer_mode,
            "reason": reason,
            "transfer_message": transfer_message,
            "has_briefing": bool(briefing_text or briefing_from_summary),
            "success": transfer_success,
        },
    )

    if transfer_success:
        return ControlResult(
            success=True,
            message="Transfer initiated",
            details={"target": target_number, "mode": transfer_mode},
        )
    # No provider transfer happened (missing sid/adapter or a provider error) —
    # don't strand the call in TRANSFERRING; it's still connected.
    await call_repo.update_call_status(
        session, call_id, status=CallStatus.IN_PROGRESS.value
    )
    return ControlResult(success=False, message="Transfer failed at provider level")


async def handoff_to_agent(
    session: AsyncSession,
    call_id: UUID,
    *,
    target_agent_id: UUID,
    reason: str | None = None,
    context_payload: dict[str, Any] | None = None,
) -> ControlResult:
    """Hand off an active call to another agent."""
    from loguru import logger

    from turncall.storage.repositories import agent_repo, call_repo

    logger.info(
        "handoff_to_agent: call_id={call_id} target_agent_id={target} reason={reason}",
        call_id=str(call_id),
        target=str(target_agent_id),
        reason=reason,
    )

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None:
        logger.error("handoff_to_agent: call not found: {id}", id=str(call_id))
        return ControlResult(success=False, message="Call not found")

    current = CallStatus(call.status)
    if current != CallStatus.IN_PROGRESS:
        logger.warning(
            "handoff_to_agent: invalid state current={current}",
            current=current.value,
        )
        return ControlResult(
            success=False,
            message=f"Can only handoff from in_progress (current: {current.value})",
        )

    target = await agent_repo.get_agent_by_id(session, target_agent_id)
    if target is None:
        logger.error(
            "handoff_to_agent: target agent not found: {id}",
            id=str(target_agent_id),
        )
        return ControlResult(
            success=False,
            message=f"Target agent not found: {target_agent_id}",
        )

    await call_repo.update_call_status(
        session,
        call_id,
        status=CallStatus.HANDED_OFF.value,
        active_agent_id=target_agent_id,
    )

    from turncall.events.emit import emit_call_event

    await emit_call_event(
        session,
        call_id=call_id,
        project_id=call.project_id,
        event_type=CallEventType.CALL_AGENT_HANDOFF,
        payload={
            "source_agent_id": str(call.active_agent_id),
            "target_agent_id": str(target_agent_id),
            "reason": reason,
            "context_payload": context_payload,
        },
    )

    await call_repo.update_call_status(
        session, call_id, status=CallStatus.IN_PROGRESS.value
    )

    return ControlResult(
        success=True,
        message="Handoff completed",
        details={
            "target_agent_id": str(target_agent_id),
            "target_assistant_name": target.name,
        },
    )


async def send_dtmf(
    session: AsyncSession,
    call_id: UUID,
    *,
    digits: str,
) -> ControlResult:
    """Send DTMF tones on an active call via Twilio."""
    from turncall.storage.repositories import call_repo

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None:
        return ControlResult(success=False, message="Call not found")

    current = CallStatus(call.status)
    if not is_active_state(current):
        return ControlResult(
            success=False,
            message=f"Call is not active (status: {current.value})",
        )

    sid = call.provider_call_sid
    if sid and sid != "webrtc":
        adapter = _get_twilio_adapter()
        if adapter:
            try:
                await adapter.send_dtmf(sid, digits)
            except Exception:
                logger.exception("twilio_dtmf_failed", call_sid=sid)
                return ControlResult(success=False, message="DTMF send failed")

    from turncall.events.emit import emit_call_event

    await emit_call_event(
        session,
        call_id=call_id,
        project_id=call.project_id,
        event_type=CallEventType.DTMF_SENT,
        payload={"digits": digits},
    )

    return ControlResult(success=True, message="DTMF sent", details={"digits": digits})


async def inject_context(
    session: AsyncSession,
    call_id: UUID,
    *,
    message: str,
    role: str = "system",
) -> ControlResult:
    """Inject a context message into an active call's LLM context."""
    from turncall.storage.repositories import call_repo

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None:
        return ControlResult(success=False, message="Call not found")

    current = CallStatus(call.status)
    if not is_active_state(current):
        return ControlResult(
            success=False,
            message=f"Call is not active (status: {current.value})",
        )

    from turncall.events.emit import emit_call_event

    await emit_call_event(
        session,
        call_id=call_id,
        project_id=call.project_id,
        event_type=CallEventType.CONTEXT_INJECTED,
        payload={"message": message, "role": role},
    )

    return ControlResult(
        success=True,
        message="Context injected",
        details={"role": role},
    )
