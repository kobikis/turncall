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
from turncall.domain.carriers import TELNYX, TWILIO
from turncall.domain.enums import CallDirection, CallEventType
from turncall.domain.models import AgentConfig
from turncall.events.server_events import send_call_init
from turncall.services.call_init_resolver import resolve_call_init
from turncall.services.limits import admit_outbound_call, release_live_call
from turncall.services.telnyx_accounts import CONNECTION_KEY
from turncall.services.telnyx_accounts import (
    account_for_project as telnyx_account_for_project,
)
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

    from turncall.services.telnyx_accounts import TelnyxAccount
    from turncall.services.twilio_accounts import TwilioAccount
    from turncall.storage.models import CallRow, PhoneNumberRow


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

    if phone_number.provider not in (TWILIO, TELNYX):
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=(
                f"Outbound calls from a {phone_number.provider} number are not "
                "supported; place them from a Twilio or Telnyx number"
            ),
        )

    account = await _carrier_account(session, phone_number, project_id, settings)
    if phone_number.provider == TELNYX:
        # The application the dial runs on, on the call record before it is
        # dialled: a Telnyx transfer dials the operator on the same one.
        metadata = {
            **metadata,
            CONNECTION_KEY: await _telnyx_connection(account, phone_number),
        }

    call_id = call_id or uuid4()
    runs_as, metadata = await _call_init(
        session,
        phone_number,
        project_id=project_id,
        agent_id=agent.id,
        call_id=call_id,
        to_number=to_number,
        metadata=metadata,
    )

    # Last, so a dial refused for any other reason holds no slot. The slot is
    # the call's from here: freed when it ends (a status callback, or the media
    # stream's hold), or at once if the dial never happens.
    await admit_outbound_call(
        session,
        project_id=project_id,
        call_id=call_id,
        from_number=phone_number.e164_number,
    )
    try:
        call, provider_call_sid = await _dial(
            session,
            call_id=call_id,
            project_id=project_id,
            agent_id=runs_as,
            phone_number=phone_number,
            account=account,
            to_number=to_number,
            metadata=metadata,
            before_carrier=before_carrier,
        )
    except BaseException:
        # Nothing was dialled: the slot was never the call's.
        await release_live_call(project_id, call_id)
        raise
    # Past here the phone is ringing, so its slot stays taken whatever
    # happens: freed by the call ending, or by its lease lapsing if nothing
    # ever holds it. Freeing it now would admit another dial into a slot a
    # live call is about to re-take — one call over the limit.
    try:
        return await _record_dial(
            session,
            call,
            provider_call_sid=provider_call_sid,
            from_number=phone_number.e164_number,
            to_number=to_number,
            agent_id=runs_as,
        )
    except Exception as exc:
        raise DialNotRecorded(call_id, provider_call_sid) from exc


async def _call_init(
    session: AsyncSession,
    number: PhoneNumberRow,
    *,
    project_id: UUID,
    agent_id: UUID,
    call_id: UUID,
    to_number: str,
    metadata: dict[str, Any],
) -> tuple[UUID | None, dict[str, Any]]:
    """The agent and metadata the call runs with (#277).

    A webhook-routed number asks its server, as on an inbound call, and the
    answer is stored where the media stream reads an inbound one's. Its agent
    wins — an agent id in the project, or an inline config (agent id None,
    ADR-0017); the dial's own variables and metadata win a clash. Any other
    number, or a call-init that fails or answers nothing usable, keeps the
    dial's agent: the dial named one, so there is always an agent to run.
    """
    if not (number.routing_target_type == "webhook" and number.server_url):
        return agent_id, metadata
    dial_variables = metadata.get("template_variables")
    if not isinstance(dial_variables, dict):
        dial_variables = {}
    response = await send_call_init(
        number.server_url,
        call_id=str(call_id),
        # Our number is the one "called", the callee the customer — the
        # roles a call-init server already reads.
        from_number=to_number,
        to_number=number.e164_number,
        transport_type="outboundPhoneCall",
        secret=number.server_url_secret,
        agent_id=str(agent_id),
        variables=dial_variables,
    )
    if not (response.success and isinstance(response.data, dict)):
        logger.warning(
            "outbound_call_init_failed: the dial's agent runs",
            call_id=str(call_id),
            error=response.error,
        )
        return agent_id, metadata
    result = await resolve_call_init(session, response.data)

    runs_as: UUID | None = agent_id
    dynamic_config = result.dynamic_config
    if dynamic_config is not None:
        try:
            AgentConfig.model_validate(dynamic_config)
            runs_as = None
        except ValueError as exc:
            # Refused before the phone rings, not at the callee's first word.
            logger.warning(
                "outbound_call_init_invalid_agent: the dial's agent runs",
                call_id=str(call_id),
                error=str(exc),
            )
            dynamic_config = None
    elif result.agent is not None:
        if result.agent.project_id == project_id:
            runs_as = result.agent.id
        else:
            logger.warning(
                "outbound_call_init_foreign_agent: the dial's agent runs",
                call_id=str(call_id),
            )

    added = {
        "dynamic_config": dynamic_config,
        "knowledge_context": result.knowledge_context,
    }
    merged = {**{k: v for k, v in added.items() if v}, **metadata}
    for key, theirs in (
        ("template_variables", result.template_variables),
        ("metadata", result.metadata),
    ):
        if theirs:
            own = metadata.get(key)
            merged[key] = {**theirs, **(own if isinstance(own, dict) else {})}
    return runs_as, merged


class DialNotRecorded(Exception):
    """The carrier placed the call, and writing that down failed. The phone
    may be ringing; the call keeps its live-call slot."""

    def __init__(self, call_id: UUID, provider_call_sid: str) -> None:
        super().__init__(
            f"call {call_id} was placed ({provider_call_sid}) but not recorded"
        )
        self.call_id = call_id
        self.provider_call_sid = provider_call_sid


async def _carrier_account(
    session: AsyncSession,
    phone_number: PhoneNumberRow,
    project_id: UUID,
    settings: Any,
) -> TwilioAccount | TelnyxAccount:
    """The account the caller-id number dials on: on Twilio the number's own,
    else the platform's; on Telnyx the project's, else the platform's."""
    if phone_number.provider == TELNYX:
        telnyx = await telnyx_account_for_project(session, project_id, settings)
        if not telnyx.api_key:
            raise ApiError(
                status_code=503,
                code=ErrorCode.NO_TELNYX_ACCOUNT,
                message=(
                    "No Telnyx account for this number: store the project's "
                    "`telnyx` credential, or set TELNYX_API_KEY"
                ),
                details={"missing_keys": [TELNYX]},
            )
        return telnyx
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
    return account


async def _dial(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    agent_id: UUID | None,
    phone_number: PhoneNumberRow,
    account: TwilioAccount | TelnyxAccount,
    to_number: str,
    metadata: dict[str, Any],
    before_carrier: Callable[[], Awaitable[None]] | None,
) -> tuple[CallRow, str]:
    """Write the call row and dial: the row, and the carrier's SID."""
    settings = get_settings()
    call = await call_repo.create_call(
        session,
        project_id=project_id,
        call_id=call_id,
        direction=CallDirection.OUTBOUND,
        from_number=phone_number.e164_number,
        to_number=to_number,
        active_agent_id=agent_id,
        metadata_json=metadata,
        carrier=phone_number.provider,
    )

    # Build webhook URLs from the configured public base — NEVER the client Host
    # header, which a caller can spoof to make the carrier fetch call TwiML (or
    # send the call's audio) to an attacker-controlled server.
    base_url = (settings.server.public_base_url or "").rstrip("/")
    if not base_url:
        raise ConflictError(
            "PUBLIC_BASE_URL must be configured to place outbound calls "
            "(the carrier needs a reachable webhook URL)."
        )

    if before_carrier is not None:
        await before_carrier()
    if phone_number.provider == TELNYX:
        provider_call_sid = await _dial_telnyx(
            account.adapter(),
            from_number=phone_number.e164_number,
            to_number=to_number,
            connection_id=metadata[CONNECTION_KEY],
            base_url=base_url,
            call_id=call.id,
        )
    else:
        provider_call_sid = await account.adapter().initiate_outbound_call(
            from_number=phone_number.e164_number,
            to_number=to_number,
            webhook_url=f"{base_url}/webhooks/twilio/voice/outbound",
            status_callback_url=f"{base_url}/webhooks/twilio/status",
        )
    return call, provider_call_sid


async def _record_dial(
    session: AsyncSession,
    call: CallRow,
    *,
    provider_call_sid: str,
    from_number: str,
    to_number: str,
    agent_id: UUID | None,
) -> CallRow:
    """Put the carrier's SID on the call row and record `call.started`."""
    updated = await call_repo.update_call_status(
        session, call.id, status="initiated", provider_call_sid=provider_call_sid
    )
    if updated is None:
        # Created in this session a moment ago; gone means something deleted it
        # mid-dial, and the carrier call is already placed.
        raise RuntimeError(
            f"outbound call {call.id} vanished after dialling {provider_call_sid}"
        )
    seq = await call_repo.get_next_sequence_number(session, call.id)
    await call_repo.create_call_event(
        session,
        call_id=call.id,
        event_type=CallEventType.CALL_STARTED,
        payload={
            "provider_call_sid": provider_call_sid,
            "from_number": from_number,
            "to_number": to_number,
            # None for call-init's inline agent (ADR-0017).
            "agent_id": str(agent_id) if agent_id else None,
            "direction": CallDirection.OUTBOUND.value,
        },
        sequence_number=seq,
    )

    logger.info(
        "outbound_call_created",
        call_id=str(call.id),
        provider_call_sid=provider_call_sid,
    )
    return updated


async def _telnyx_connection(account: Any, phone_number: PhoneNumberRow) -> str:
    """The Call Control application the caller-id number is on. Asked before
    the call is admitted, so a number that cannot dial holds no slot."""
    from turncall.adapters.telephony.telnyx_adapter import (
        TelnyxApiError,
        TelnyxNumberError,
    )

    try:
        return str(await account.adapter().connection_for(phone_number.e164_number))
    except TelnyxNumberError as exc:
        raise ConflictError(str(exc)) from exc
    except TelnyxApiError as exc:
        raise _telnyx_refused(exc) from exc


def _telnyx_refused(exc: Exception) -> ApiError:
    return ApiError(status_code=502, code=ErrorCode.TELNYX_ERROR, message=str(exc))


async def _dial_telnyx(
    adapter: Any,
    *,
    from_number: str,
    to_number: str,
    connection_id: str,
    base_url: str,
    call_id: UUID,
) -> str:
    """Dial on Telnyx: its events to TurnCall's Call Control webhook, and the
    callee's media straight to the call's stream once they answer (Telnyx
    opens it; there is no answer step on an outbound call)."""
    from turncall.adapters.telephony.telnyx_adapter import TelnyxApiError
    from turncall.webhooks.telnyx_media_stream import media_stream_path

    ws_base = base_url.replace("http", "ws", 1)  # https -> wss, http -> ws
    try:
        return str(
            await adapter.initiate_outbound_call(
                from_number=from_number,
                to_number=to_number,
                webhook_url=f"{base_url}/webhooks/telnyx/voice",
                connection_id=connection_id,
                stream_url=f"{ws_base}{media_stream_path(call_id)}",
            )
        )
    except TelnyxApiError as exc:
        raise _telnyx_refused(exc) from exc
