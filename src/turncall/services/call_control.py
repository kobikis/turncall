"""Shared call control primitives.

Used by both built-in tools (via Pipecat function calling)
and live call control API endpoints. The REST adapter is resolved per call,
from the call's own carrier and account — never passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.adapters.telephony.base import UnsupportedCarrierAction
from turncall.domain.call_state import is_active_state, is_valid_transition
from turncall.domain.enums import CallEventType, CallStatus


@dataclass(frozen=True)
class ControlResult:
    """Result of a call control action."""

    success: bool
    message: str
    details: dict[str, Any] | None = None


async def _carrier_adapter(session: AsyncSession, call: Any) -> Any | None:
    """The REST adapter of this call's own carrier (ADR-0022), on its account.

    Telnyx: the project's `telnyx` credential, else the platform's key.
    Asterisk: the project's own Asterisk, else the platform's (#209).
    Twilio: the number's own account if bound with one, else the platform's.
    None when no account exists to act with.
    """
    from turncall.config import get_settings
    from turncall.domain.carriers import ASTERISK, TELNYX

    settings = get_settings()
    if call.provider == TELNYX:
        from turncall.services.telnyx_accounts import account_for_project

        telnyx = await account_for_project(session, call.project_id, settings)
        return telnyx.adapter() if telnyx.api_key else None
    if call.provider == ASTERISK:
        from turncall.services.asterisk_accounts import (
            account_for_project as asterisk_account,
        )

        account: Any = await asterisk_account(session, call.project_id, settings)
    else:
        from turncall.services.twilio_accounts import account_for_call

        account = await account_for_call(session, call, settings)
    return account.adapter() if account else None


async def _endable_call(session: AsyncSession, call_id: UUID) -> Any:
    """The call row if it can be ended now, else a ControlResult saying why not."""
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
    return call


async def _record_control_end(
    session: AsyncSession, call_id: UUID, reason: str
) -> None:
    """The `call.ended` event `infer_ended_reason` reads as the agent's hangup."""
    from turncall.storage.repositories import call_repo

    seq = await call_repo.get_next_sequence_number(session, call_id)
    await call_repo.create_call_event(
        session,
        call_id=call_id,
        event_type=CallEventType.CALL_ENDED,
        payload={"reason": reason, "source": "control"},
        sequence_number=seq,
    )


async def begin_assistant_end(
    session: AsyncSession, call_id: UUID, *, reason: str
) -> ControlResult:
    """Record that the agent is ending the call, without hanging up.

    The tool path: the caller has to hear the goodbye, which the model gives
    in reply to this result. So only the record is written here — before
    anything closes, so a carrier status callback racing the hangup cannot
    make the call read as the caller's — and the pipeline ends itself after
    that reply has played (`orchestrator/hangup.py`), which hangs up.
    Status, `ended_at` and post-call processing come from `finalize_call` then.
    """
    call = await _endable_call(session, call_id)
    if isinstance(call, ControlResult):
        return call
    await _record_control_end(session, call_id, reason)
    # Read by the model, which replies next: that reply is the goodbye.
    return ControlResult(
        success=True,
        message=(
            "The call ends after your next reply. Say a short goodbye now, "
            "or reply with nothing if you already said goodbye."
        ),
    )


async def end_call(
    session: AsyncSession,
    call_id: UUID,
    *,
    reason: str = "normal",
) -> ControlResult:
    """End an active call now: hang up through its carrier and update internal state.

    The live call control API. A tool ending its own call goes through
    `begin_assistant_end` instead, so its goodbye is heard.
    """
    from turncall.storage.repositories import call_repo

    call = await _endable_call(session, call_id)
    if isinstance(call, ControlResult):
        return call

    # Hang up through the call's carrier
    sid = call.provider_call_sid
    if sid and sid != "webrtc":
        adapter = await _carrier_adapter(session, call)
        if adapter:
            try:
                await adapter.end_call(sid)
            except Exception:
                logger.exception("carrier_hangup_failed", call_sid=sid)

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

    await _record_control_end(session, call_id, reason)

    # Trigger post-call processing (transcript, recording, analysis, webhook)
    # The comprehensive call.ended webhook is dispatched after analysis completes.
    try:
        from turncall.services.call_analysis_trigger import (
            config_for_call,
            trigger_post_call_analysis,
        )
        from turncall.storage.database import get_session_factory

        # Not gated on active_agent_id: a call running an inline agent has none.
        config = await config_for_call(session, call)
        if config is not None:
            trigger_post_call_analysis(
                get_session_factory(),
                call_id,
                call.project_id,
                config,
            )
    except Exception:
        logger.exception("analysis_trigger_error", call_id=str(call_id))

    return ControlResult(success=True, message="Call ended")


async def do_not_call(session: AsyncSession, call_id: UUID) -> ControlResult:
    """Suppress the caller's number, record `call.opted_out`, start ending (#228).

    The caller is whoever is not us: the dialling party inbound, the dialled
    one outbound. A call with no E.164 number on that side (WebRTC, a WhatsApp
    id) has nothing to suppress, so it is refused and left running — ending it
    would tell the caller they were opted out when they were not.
    """
    import re

    from turncall.api.v1.schemas import E164_PATTERN
    from turncall.events.emit import emit_call_event
    from turncall.storage.repositories import call_repo, suppression_repo

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None:
        return ControlResult(success=False, message="Call not found")

    number = call.from_number if call.direction == "inbound" else call.to_number
    if not number or not re.match(E164_PATTERN, number):
        return ControlResult(
            success=False, message="This call has no phone number to opt out"
        )

    # The opt-out first, and committed on its own: it must hold even if ending
    # the call does not (already ended, carrier hangup failed, or a raise that
    # would otherwise roll it back with the rest of the session).
    await suppression_repo.add(session, project_id=call.project_id, number=number)
    await emit_call_event(
        session,
        call_id=call_id,
        project_id=call.project_id,
        event_type=CallEventType.CALL_OPTED_OUT,
        payload={"number": number},
    )
    await session.commit()
    ending = await begin_assistant_end(session, call_id, reason="opted_out")
    return ControlResult(
        # Success is the opt-out, which holds either way; `call_ended` says
        # whether the tool may go on to end the call.
        success=True,
        message=f"Number opted out; {ending.message}",
        details={"number": number, "call_ended": ending.success},
    )


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

    from turncall.domain.carriers import TELNYX

    # A Telnyx transfer is steered by Telnyx's webhooks, which read the intent
    # whether or not a public URL exists (services/telnyx_transfer.py).
    on_telnyx = call.provider == TELNYX
    intent = transfer_svc.TransferIntent(
        target_number=target_number,
        transfer_mode=transfer_mode,
        transfer_message=transfer_message,
        briefing_text=briefing_text,
        briefing_from_summary=briefing_from_summary,
        fallback_message=fallback_message,
    )
    whisper_url = action_url = amd_url = None
    if base_url:
        urls = transfer_svc.transfer_callback_urls(base_url, call_id)
        whisper_url = urls["whisper"] if warm else None
        action_url = urls["result"] if fallback_message else None
        amd_url = urls["amd"]  # AMD notify on both modes
    if base_url or on_telnyx:
        await transfer_svc.store_transfer_intent(call_id, intent)

    # Execute the transfer on the call's carrier
    sid = call.provider_call_sid
    transfer_success = False
    refusal: str | None = None
    if sid and sid != "webrtc":
        adapter = await _carrier_adapter(session, call)
        if adapter:
            try:
                if on_telnyx:
                    from turncall.services import telnyx_transfer

                    # Same AMD rule as Twilio's: notify where Twilio would.
                    await telnyx_transfer.start(
                        adapter, call, intent, detect_machine=amd_url is not None
                    )
                else:
                    await adapter.transfer_call(
                        sid,
                        target_number,
                        transfer_message=transfer_message,
                        whisper_url=whisper_url,
                        action_url=action_url,
                        amd_callback_url=amd_url,
                    )
                transfer_success = True
            except UnsupportedCarrierAction as exc:
                refusal = str(exc)
            except Exception:
                logger.exception(
                    "carrier_transfer_failed", call_sid=sid, target=target_number
                )
    if on_telnyx and not transfer_success:
        # Its media edge reads a parked intent as "transferring, leave the call
        # up" when the stream ends.
        await transfer_svc.clear_transfer_intent(call_id)

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
    return ControlResult(
        success=False, message=refusal or "Transfer failed at provider level"
    )


async def handoff_to_agent(
    session: AsyncSession,
    call_id: UUID,
    *,
    target_agent_id: UUID,
    reason: str | None = None,
    context_payload: dict[str, Any] | None = None,
) -> ControlResult:
    """Commit a handoff to the call record: `HANDED_OFF`, the new active agent,
    the `call.agent_handoff` event.

    Only the call itself calls this, from `tool_bridge.run_handoff`, between
    preparing the switch and applying it to the pipeline (#144). The API asks
    the call to do that (`request_handoff`) instead of writing the record on
    its own, which left the record handed off while the live pipeline kept the
    previous agent (#216).
    """
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

    target = await agent_repo.get_agent_by_id(
        session, target_agent_id, project_id=call.project_id
    )
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


async def request_handoff(
    call_id: UUID,
    *,
    target_agent_id: UUID,
    reason: str | None = None,
    context_payload: dict[str, Any] | None = None,
) -> ControlResult:
    """Ask the live call to hand itself off to another agent, and report what
    it applied.

    The call runs the same prepare / commit / apply path the model's
    `handoff_to_agent` tool does, so the record and the pipeline agree. Raises
    `CallNotLive` / `CallDidNotAcknowledge` like `inject_context`.
    """
    from turncall.services import live_call_channel
    from turncall.storage.redis import get_redis

    ack = await live_call_channel.request(
        get_redis(),
        call_id,
        live_call_channel.HANDOFF,
        {
            "agent_id": str(target_agent_id),
            "reason": reason,
            "context": context_payload,
        },
    )
    if not ack.ok:
        return ControlResult(success=False, message=ack.error or "Refused by the call")
    applied = ack.applied or {}
    return ControlResult(
        success=True,
        message=f"The call is now running agent '{applied.get('target_assistant_name')}'",
        details=applied,
    )


async def send_dtmf(
    session: AsyncSession,
    call_id: UUID,
    *,
    digits: str,
) -> ControlResult:
    """Send DTMF tones on an active call through its carrier."""
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
        adapter = await _carrier_adapter(session, call)
        if adapter:
            try:
                await adapter.send_dtmf(sid, digits)
            except Exception:
                logger.exception("carrier_dtmf_failed", call_sid=sid)
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
    """Add a message to a live call's LLM context, through the call itself.

    Only the task running the call can change its context, so this asks it
    over the call's control channel (#215) and reports what it answered.
    Raises `CallNotLive` when nothing is running the call and
    `CallDidNotAcknowledge` when it does not answer in time — never a success
    the call did not confirm.
    """
    from turncall.services import live_call_channel
    from turncall.storage.redis import get_redis
    from turncall.storage.repositories import call_repo

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None:
        return ControlResult(success=False, message="Call not found")

    ack = await live_call_channel.request(
        get_redis(),
        call_id,
        live_call_channel.MESSAGE,
        {"message": message, "role": role},
    )
    if not ack.ok:
        return ControlResult(success=False, message=ack.error or "Refused by the call")

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
        message="Message added to the call's context",
        details=ack.applied,
    )
