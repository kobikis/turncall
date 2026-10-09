"""The one way an outbound call is placed (#213).

Every outbound call goes through `place_outbound_call` — the API route today,
campaigns next. It is where the concurrency limit, the calls-per-second limit
and the suppression list hook in, so none of them can be bypassed by a second
dial path; `tests/unit/test_outbound_dial.py` fails if one appears.

A transfer is not an outbound call: it adds a leg to a call that already
exists and has no call row of its own, so the suppression list does not
apply to its target (#227).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from loguru import logger

from turncall.api.errors import ApiError, ConflictError, ErrorCode, NotFoundError
from turncall.config import get_settings
from turncall.domain.carriers import TWILIO
from turncall.domain.enums import CallDirection, CallEventType
from turncall.services.limits import admit_outbound_call, release_live_call
from turncall.services.twilio_accounts import account_for_number
from turncall.storage.repositories import (
    agent_repo,
    call_repo,
    phone_number_repo,
    suppression_repo,
)
from turncall.storage.repositories.credential_repo import CredentialKeyError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.services.twilio_accounts import TwilioAccount
    from turncall.storage.models import AgentRow, CallRow, PhoneNumberRow


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
    call_id: UUID | None = None,
    before_carrier: Callable[[], Awaitable[None]] | None = None,
) -> CallRow:
    """Dial `to_number` from one of the project's numbers, as `agent_id`.

    `call_id` is the id a campaign reserved its slots under
    (`limits.reserve_dial`), which the admission here then finds already
    taken; the limits are still checked, never skipped. Returns the call row,
    `initiated` and carrying the carrier's call SID.

    `before_carrier` runs after every refusal has had its chance and just
    before the carrier is asked: past it, a phone may ring. A campaign records
    that point there (#232), so a worker that dies on either side of it is
    known to have dialled or not; raising from it dials nothing.
    """
    settings = get_settings()

    # Before anything is written: a refused dial leaves no call row behind.
    if await suppression_repo.is_suppressed(
        session, project_id=project_id, number=to_number
    ):
        raise ApiError(
            status_code=409,
            code=ErrorCode.NUMBER_SUPPRESSED,
            message=f"{to_number} is on this project's suppression list",
            details={"to_number": to_number},
        )

    agent = await agent_repo.get_agent_by_id(session, agent_id, project_id=project_id)
    if agent is None:
        raise NotFoundError("Agent", str(agent_id))

    phone_number = await phone_number_repo.get_by_id(
        session, from_number_id, project_id=project_id
    )
    if phone_number is None:
        raise NotFoundError("PhoneNumber", str(from_number_id))

    if phone_number.provider != TWILIO:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=(
                f"Outbound calls from a {phone_number.provider} number are not "
                "supported; place them from a Twilio number"
            ),
        )

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

    # Last, so a dial refused for any other reason holds no slot. The slot is
    # the call's from here: freed when it ends (a status callback, or the media
    # stream's hold), or at once if the dial never happens.
    call_id = call_id or uuid4()
    await admit_outbound_call(
        session,
        project_id=project_id,
        call_id=call_id,
        from_number=phone_number.e164_number,
    )
    try:
        return await _dial(
            session,
            call_id=call_id,
            project_id=project_id,
            agent=agent,
            phone_number=phone_number,
            account=account,
            to_number=to_number,
            metadata=metadata,
            before_carrier=before_carrier,
        )
    except BaseException:
        await release_live_call(project_id, call_id)
        raise


async def _dial(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    agent: AgentRow,
    phone_number: PhoneNumberRow,
    account: TwilioAccount,
    to_number: str,
    metadata: dict[str, Any],
    before_carrier: Callable[[], Awaitable[None]] | None,
) -> CallRow:
    """Write the call row, dial, and record the carrier's SID on it."""
    settings = get_settings()
    call = await call_repo.create_call(
        session,
        project_id=project_id,
        call_id=call_id,
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

    if before_carrier is not None:
        await before_carrier()
    provider_call_sid = await account.adapter().initiate_outbound_call(
        from_number=phone_number.e164_number,
        to_number=to_number,
        webhook_url=f"{base_url}/webhooks/twilio/voice/outbound",
        status_callback_url=f"{base_url}/webhooks/twilio/status",
    )

    updated = await call_repo.update_call_status(
        session, call.id, status="initiated", provider_call_sid=provider_call_sid
    )
    if updated is None:
        # Created in this session a moment ago; gone means something deleted it
        # mid-dial, and the carrier call is already placed.
        raise RuntimeError(
            f"outbound call {call.id} vanished after dialling {provider_call_sid}"
        )
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
            "direction": CallDirection.OUTBOUND.value,
        },
        sequence_number=seq,
    )

    logger.info(
        "outbound_call_created",
        call_id=str(call.id),
        provider_call_sid=provider_call_sid,
    )
    return call
