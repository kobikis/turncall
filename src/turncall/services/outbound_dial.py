"""The one way an outbound call is placed (#213).

Every outbound call goes through `place_outbound_call` — the API route today,
campaigns next. It is where the concurrency limit, the calls-per-second limit
and the suppression list hook in, so none of them can be bypassed by a second
dial path; `tests/unit/test_outbound_dial.py` fails if one appears.

A transfer is not an outbound call: it adds a leg to a call that already
exists, to a target the agent's own config names, and has no call row of its
own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger

from turncall.api.errors import ApiError, ConflictError, ErrorCode, NotFoundError
from turncall.config import get_settings
from turncall.domain.enums import CallDirection, CallEventType
from turncall.services.twilio_accounts import account_for_number
from turncall.storage.repositories import agent_repo, call_repo, phone_number_repo
from turncall.storage.repositories.credential_repo import CredentialKeyError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.storage.models import CallRow


# ponytail: raises the API's own errors, which every caller so far answers
# with; a non-HTTP caller (a campaign worker) reads `.code` off them.
async def place_outbound_call(
    session: AsyncSession,
    *,
    project_id: UUID,
    agent_id: UUID,
    from_number_id: UUID,
    to_number: str,
    metadata: dict[str, Any],
) -> CallRow:
    """Dial `to_number` from one of the project's numbers, as `agent_id`.

    Returns the call row, `initiated` and carrying the carrier's call SID.
    """
    settings = get_settings()

    agent = await agent_repo.get_agent_by_id(session, agent_id, project_id=project_id)
    if agent is None:
        raise NotFoundError("Agent", str(agent_id))

    phone_number = await phone_number_repo.get_by_id(
        session, from_number_id, project_id=project_id
    )
    if phone_number is None:
        raise NotFoundError("PhoneNumber", str(from_number_id))

    # The caller-id number's own Twilio account, else the platform's.
    try:
        account = account_for_number(phone_number, settings)
    except CredentialKeyError as exc:
        # The number's own token is sealed under a key this process lacks.
        raise ApiError(
            status_code=503, code=ErrorCode.CREDENTIAL_KEY_UNAVAILABLE, message=str(exc)
        ) from exc
    if account is None:
        raise ApiError(
            status_code=503,
            code=ErrorCode.NO_TWILIO_ACCOUNT,
            message=(
                "No Twilio account for this number: bind it with its own "
                "twilio_account_sid + twilio_auth_token, or set TWILIO_ACCOUNT_SID "
                "and TWILIO_AUTH_TOKEN"
            ),
        )

    call = await call_repo.create_call(
        session,
        project_id=project_id,
        direction=CallDirection.OUTBOUND,
        from_number=phone_number.e164_number,
        to_number=to_number,
        active_agent_id=agent.id,
        metadata_json=metadata,
    )

    # Build webhook URLs from the configured public base — NEVER the client Host
    # header, which a caller can spoof to make Twilio fetch call TwiML from an
    # attacker-controlled server using our Twilio credentials.
    base_url = (settings.server.public_base_url or "").rstrip("/")
    if not base_url:
        raise ConflictError(
            "PUBLIC_BASE_URL must be configured to place outbound calls "
            "(Twilio needs a reachable webhook URL)."
        )

    provider_call_sid = await account.adapter().initiate_outbound_call(
        from_number=phone_number.e164_number,
        to_number=to_number,
        webhook_url=f"{base_url}/webhooks/twilio/voice/outbound",
        status_callback_url=f"{base_url}/webhooks/twilio/status",
    )

    updated = await call_repo.update_call_status(
        session, call.id, status="initiated", provider_call_sid=provider_call_sid
    )
    # The row was created in this session a moment ago; it cannot be gone.
    assert updated is not None
    call = updated
    seq = await call_repo.get_next_sequence_number(session, call.id)
    await call_repo.create_call_event(
        session,
        call_id=call.id,
        event_type=CallEventType.CALL_STARTED,
        payload={
            "provider_call_sid": provider_call_sid,
            "from_number": phone_number.e164_number,
            "to_number": to_number,
            "agent_id": str(agent.id),
            "direction": "outbound",
        },
        sequence_number=seq,
    )

    logger.info(
        "outbound_call_created",
        call_id=str(call.id),
        provider_call_sid=provider_call_sid,
    )
    return call
