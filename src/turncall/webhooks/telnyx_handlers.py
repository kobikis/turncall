"""Telnyx's inbound edge: Call Control webhooks (ADR-0022).

A Telnyx number's Call Control application posts every call event to one URL,
`/webhooks/telnyx/voice`, as JSON `{"data": {"event_type", "payload"}}` signed
with the account's Ed25519 key. An incoming `call.initiated` is answered the
Call Control way — a REST `answer` command whose `stream_url` points the call's
media at `/ws/telnyx/media-stream/{call_id}` (`telnyx_media_stream.py`), where
the shared media entry point takes over. An outgoing call (#218) needs nothing
here until it ends: its dial already named that stream. `call.hangup` closes a
call no pipeline ever ran for — including a dial nobody answered — and one
that ran closes itself.

Whose key verifies a webhook is decided by whose call or number it names: the
project's own `telnyx` credential, else the platform's TELNYX_PUBLIC_KEY. No
key means no verification and the webhook is refused — there is no unsigned
dev mode on this carrier.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.deps import DbSession
from turncall.config import get_settings
from turncall.domain.carriers import TELNYX
from turncall.domain.enums import CallDirection, CallEventType, CallStatus
from turncall.services.limits import release_live_call
from turncall.services.telnyx_accounts import TelnyxAccount, account_for_project
from turncall.storage.models import CallRow, PhoneNumberRow
from turncall.storage.repositories import call_repo, phone_number_repo
from turncall.webhooks.telnyx_signature import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify_telnyx_signature,
)

router = APIRouter(prefix="/webhooks/telnyx", tags=["telnyx-webhooks"])

# A call in these states has had no media stream yet, so no pipeline will
# finalize it: a hangup now is the only end it gets.
_NOT_YET_STREAMING = {CallStatus.INITIATED.value, CallStatus.RINGING.value}

# How a call TurnCall dialled ends when nobody picked up, by Telnyx's
# `hangup_cause` — what Twilio's `no-answer`/`busy`/`failed` statuses say, and
# what a campaign's retry policy reads. A cause not here (the callee answered
# and hung up before the stream opened) ends the call as completed.
_UNANSWERED = {
    "timeout": CallStatus.NO_ANSWER,
    "no_answer": CallStatus.NO_ANSWER,
    "user_busy": CallStatus.BUSY,
    "call_rejected": CallStatus.BUSY,
    "not_found": CallStatus.FAILED,
}


def _stream_url(request: Request, call_id: UUID) -> str:
    """Where Telnyx opens the call's media stream. wss:// behind HTTPS (ngrok,
    production), detected via X-Forwarded-Proto, as for Twilio's stream."""
    host = request.headers.get("host", "localhost:8000")
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_https = forwarded_proto == "https" or get_settings().is_production
    return f"{'wss' if is_https else 'ws'}://{host}/ws/telnyx/media-stream/{call_id}"


async def _whose(
    session: AsyncSession, payload: dict[str, Any]
) -> tuple[CallRow | None, PhoneNumberRow | None]:
    """The Telnyx call this event is about, else the Telnyx number on it — the
    one dialled on an incoming call, the caller id on an outgoing one (whose
    first events can beat the dial's own write of its `call_control_id`)."""
    call_control_id = payload.get("call_control_id") or ""
    call = (
        await call_repo.get_call_by_provider_sid(session, call_control_id)
        if call_control_id
        else None
    )
    if call is not None and call.provider == TELNYX:
        return call, None
    ours = payload.get("to" if payload.get("direction") == "incoming" else "from")
    number = await phone_number_repo.get_by_e164(session, ours or "")
    return None, number if number is not None and number.provider == TELNYX else None


@router.post("/voice")
async def call_control_webhook(request: Request, session: DbSession) -> Response:
    """Every Call Control event for a Telnyx number. 200 for anything verified,
    handled or not — Telnyx retries a webhook that is not acknowledged."""
    body = await request.body()
    try:
        event = json.loads(body)["data"]
        event_type = str(event["event_type"])
        payload = dict(event.get("payload") or {})
    except (ValueError, KeyError, TypeError):
        return Response(status_code=400)

    call, number = await _whose(session, payload)
    project_id = call.project_id if call else number.project_id if number else None
    account = await account_for_project(session, project_id, get_settings())
    if not verify_telnyx_signature(
        account.public_key,
        request.headers.get(SIGNATURE_HEADER, ""),
        request.headers.get(TIMESTAMP_HEADER, ""),
        body,
    ):
        logger.warning("telnyx_invalid_signature", event_type=event_type)
        return Response(status_code=403)

    if event_type == "call.initiated" and payload.get("direction") == "incoming":
        # A retried webhook names a call already opened: answer it once.
        if call is None:
            await _answer_inbound(request, session, account, number, payload)
    elif event_type == "call.hangup" and call is not None:
        await _on_hangup(session, call, payload)
    return Response(status_code=200)


async def _answer_inbound(
    request: Request,
    session: AsyncSession,
    account: TelnyxAccount,
    number: PhoneNumberRow | None,
    payload: dict[str, Any],
) -> None:
    call_control_id = str(payload.get("call_control_id") or "")
    from_number = str(payload.get("from") or "")
    to_number = str(payload.get("to") or "")
    logger.info(
        "telnyx_inbound_call",
        call_control_id=call_control_id,
        from_number=from_number,
        to_number=to_number,
    )
    if number is None:
        logger.warning("telnyx_inbound_unknown_number", to_number=to_number)
        await _hang_up(account, call_control_id)
        return

    from turncall.services.inbound_call import InboundRefusal, open_inbound_call

    call = await open_inbound_call(
        session,
        number,
        carrier=TELNYX,
        from_number=from_number,
        to_number=to_number,
        provider_call_sid=call_control_id,
    )
    if isinstance(call, InboundRefusal):
        if call.busy:
            await _reject_busy(account, call_control_id)
        else:
            await _hang_up(account, call_control_id)
        return
    # Committed before answering: the media stream Telnyx opens next reads the
    # call row in a session of its own.
    await session.commit()

    if not account.api_key:
        from turncall.services.credentials import MissingProviderKeyError

        await _fail(session, call, MissingProviderKeyError([TELNYX]), [TELNYX])
        return
    try:
        await account.adapter().answer_call(
            call_control_id, stream_url=_stream_url(request, call.id)
        )
    except Exception as exc:
        logger.exception("telnyx_answer_failed", call_id=str(call.id))
        await _fail(session, call, exc, [])


async def _reject_busy(account: TelnyxAccount, call_control_id: str) -> None:
    """Refuse a call over the project's live-call limit unanswered, busy (#224).
    Best-effort, like `_hang_up`: `call.rejected` has already gone out."""
    if not (account.api_key and call_control_id):
        return
    try:
        await account.adapter().reject_call(call_control_id, cause="USER_BUSY")
    except Exception:
        logger.exception("telnyx_reject_failed", call_control_id=call_control_id)


async def _hang_up(account: TelnyxAccount, call_control_id: str) -> None:
    """Hang up a call nobody will take. Best-effort: a call left ringing is
    the carrier's to time out."""
    if not (account.api_key and call_control_id):
        return
    try:
        await account.adapter().end_call(call_control_id)
    except Exception:
        logger.exception("telnyx_hangup_failed", call_control_id=call_control_id)


async def _fail(
    session: AsyncSession, call: CallRow, error: Exception, missing_keys: list[str]
) -> None:
    """A call that could not be answered: failed, with why, and `call.ended`."""
    from turncall.events.emit import emit_call_event
    from turncall.orchestrator.call_session import finalize_call, readable_error
    from turncall.storage.database import get_session_factory

    status = getattr(error, "status", None)
    await emit_call_event(
        session,
        call_id=call.id,
        project_id=call.project_id,
        event_type=CallEventType.ERROR_RAISED,
        payload={
            "stage": "carrier_answer",
            "error": readable_error(error),
            **({"missing_keys": missing_keys} if missing_keys else {}),
            **(
                {"provider": TELNYX, "key_rejected": True}
                if status in (401, 403)
                else {}
            ),
        },
    )
    await session.commit()
    # No media stream will ever hold the slot the call was admitted with.
    await release_live_call(call.project_id, call.id)
    await finalize_call(get_session_factory(), call.id, failed=True)


async def _on_hangup(
    session: AsyncSession, call: CallRow, payload: dict[str, Any]
) -> None:
    """Telnyx says the call is over. Recorded on the call's event log as
    Twilio's `completed` status callback is (stored, not dispatched — the
    comprehensive `call.ended` comes from post-call processing).

    A call no pipeline ever ran for — the caller hung up before the media
    stream opened — is finalized here, since nothing else will. One that has a
    pipeline is finalized by it, recording and transcript complete.
    """
    seq = await call_repo.get_next_sequence_number(session, call.id)
    await call_repo.create_call_event(
        session,
        call_id=call.id,
        event_type=CallEventType.CALL_ENDED,
        payload={
            "provider_call_sid": call.provider_call_sid,
            "hangup_cause": payload.get("hangup_cause"),
            "hangup_source": payload.get("hangup_source"),
            "sip_hangup_cause": payload.get("sip_hangup_cause"),
            "from_number": call.from_number,
            "to_number": call.to_number,
        },
        sequence_number=seq,
    )
    await session.commit()
    # The carrier says the call is over, whatever our state machine thinks —
    # as Twilio's terminal status callback does (#224).
    await release_live_call(call.project_id, call.id)
    if call.status not in _NOT_YET_STREAMING:
        return
    unanswered = _UNANSWERED.get(str(payload.get("hangup_cause") or ""))
    if unanswered is not None and call.direction == CallDirection.OUTBOUND.value:
        await _not_answered(session, call, unanswered)
        return
    from turncall.orchestrator.call_session import finalize_call
    from turncall.storage.database import get_session_factory

    await finalize_call(get_session_factory(), call.id, failed=False)


async def _not_answered(
    session: AsyncSession, call: CallRow, status: CallStatus
) -> None:
    """A dialled call nobody picked up: its terminal status, as Twilio's
    status callback records one. A failed dial also gets `call.failed`, which
    is what reads it back as `telephony_failed`."""
    await call_repo.update_call_status(
        session, call.id, status=status.value, ended_at=datetime.now(UTC)
    )
    if status == CallStatus.FAILED:
        from turncall.events.emit import emit_call_event

        await emit_call_event(
            session,
            call_id=call.id,
            project_id=call.project_id,
            event_type=CallEventType.CALL_FAILED,
            payload={"provider_call_sid": call.provider_call_sid},
        )
    await session.commit()
