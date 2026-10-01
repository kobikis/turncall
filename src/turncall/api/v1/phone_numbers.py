"""Phone number management endpoints."""

import secrets as secrets_mod
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from loguru import logger

from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ConflictError, ErrorCode, NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.phone_numbers import (
    BindPhoneNumberRequest,
    PhoneNumberResponse,
    RoutingResponse,
    SetRoutingWeightsRequest,
    TwilioAccountFields,
    UpdatePhoneNumberRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.config.settings import Settings, get_settings
from turncall.services import twilio_accounts
from turncall.services.twilio_accounts import TwilioAccount
from turncall.storage.repositories import credential_repo, phone_number_repo
from turncall.storage.repositories.phone_number_repo import StoredTwilioAccount

router = APIRouter(prefix="/phone-numbers", tags=["phone-numbers"])


@router.post("", status_code=201)
async def bind_phone_number(
    body: BindPhoneNumberRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Bind a Twilio phone number to a routing target."""
    existing = await phone_number_repo.get_by_e164(session, body.e164_number)
    if existing is not None:
        raise ConflictError(
            f"Phone number {body.e164_number} is already bound",
            details={"existing_project_id": str(existing.project_id)},
        )

    server_url_secret = secrets_mod.token_urlsafe(32) if body.server_url else None
    settings = get_settings()
    number_sid = body.external_number_sid
    own: StoredTwilioAccount | None = None
    if body.has_own_twilio:
        number_sid, own = await _own_twilio(
            body, body.e164_number, body.external_number_sid, settings
        )

    row = await phone_number_repo.bind_phone_number(
        session,
        project_id=auth.project_id,
        external_number_sid=number_sid,
        e164_number=body.e164_number,
        routing_target_type=body.routing_target_type,
        routing_target_id=body.routing_target_id,
        server_url=body.server_url,
        server_url_secret=server_url_secret,
        sms_enabled=body.sms_enabled,
        metadata_json=body.metadata,
        twilio_account_sid=own.account_sid if own else None,
        twilio_auth_token_ciphertext=own.token_ciphertext if own else None,
    )

    configured = await _configure_twilio_webhooks(row, body.sms_enabled)

    data = PhoneNumberResponse.from_row(row).model_dump()
    # False = TurnCall did NOT point the Twilio number at itself (missing
    # PUBLIC_BASE_URL/credentials or a Twilio error) — the number won't receive
    # calls until its Voice webhook is set. Surfaced so clients can warn.
    data["twilio_webhooks_configured"] = configured
    return ok(data)


def _unprocessable(message: str) -> ApiError:
    return ApiError(status_code=422, code=ErrorCode.VALIDATION_ERROR, message=message)


async def _own_twilio(
    body: TwilioAccountFields, e164: str, given_sid: str | None, settings: Settings
) -> tuple[str, StoredTwilioAccount]:
    """Prove the supplied Twilio account owns the number, and seal its token.

    Returns the number's PN SID and the account as it will be stored. Checked
    here, at write time, rather than when the first call fails to arrive.
    """
    from twilio.base.exceptions import TwilioRestException

    account = TwilioAccount(body.twilio_account_sid or "", body.twilio_auth_token or "")
    try:
        found = await account.adapter().find_number_sid(e164)
    except TwilioRestException as exc:
        if exc.status in (401, 403):
            raise _unprocessable(
                "Twilio rejected this Account SID / Auth Token pair"
            ) from exc
        raise ApiError(
            status_code=502, code=ErrorCode.TWILIO_ERROR, message=f"Twilio: {exc.msg}"
        ) from exc
    if found is None:
        raise _unprocessable(
            f"{e164} is not a phone number on Twilio account {account.account_sid}"
        )
    if given_sid and given_sid != found:
        raise _unprocessable(
            f"{e164} on that Twilio account is {found}, not {given_sid}"
        )
    try:
        ciphertext = twilio_accounts.encrypt_token(account.auth_token, settings)
    except credential_repo.CredentialKeyError as exc:
        raise ApiError(
            status_code=503, code=ErrorCode.CREDENTIAL_KEY_UNAVAILABLE, message=str(exc)
        ) from exc
    return found, StoredTwilioAccount(account.account_sid, ciphertext)


async def _configure_twilio_webhooks(row: Any, sms_enabled: bool) -> bool:
    """Point the Twilio number's voice + status (and SMS, if enabled) webhooks at
    TurnCall, so inbound calls reach us and finalize on hangup. Needs PUBLIC_BASE_URL
    (Twilio must reach us) + Twilio credentials. Best-effort: a Twilio failure logs
    a warning but does not fail the bind — the routing row is already recorded.
    Returns True iff Twilio was actually configured. Uses the number's own
    Twilio account when it has one, else the platform's."""
    settings = get_settings()
    external_number_sid = row.external_number_sid
    base = (settings.server.public_base_url or "").rstrip("/")
    account = twilio_accounts.account_for_number(row, settings)
    if not (base and account):
        logger.warning(
            "twilio_webhook_not_configured: set PUBLIC_BASE_URL + Twilio creds "
            "(number {sid} left unconfigured — set its Voice webhook manually)",
            sid=external_number_sid,
        )
        return False

    adapter = account.adapter()
    try:
        await adapter.configure_number_webhook(
            external_number_sid,
            voice_url=f"{base}/webhooks/twilio/voice/inbound",
            status_callback_url=f"{base}/webhooks/twilio/status",
        )
        # Always set sms_url — cleared when SMS is disabled, so toggling SMS
        # off actually stops Twilio from posting inbound texts.
        await adapter.configure_sms_webhook(
            external_number_sid,
            sms_url=f"{base}/webhooks/twilio/sms/inbound" if sms_enabled else "",
        )
        return True
    except Exception as exc:  # don't fail the bind on a Twilio hiccup
        logger.warning(
            "twilio_webhook_config_failed for {sid}: {err}",
            sid=external_number_sid,
            err=exc,
        )
        return False


@router.get("")
async def list_phone_numbers(
    auth: Auth,
    session: DbSession,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """List phone numbers for the authenticated project."""
    offset = (page - 1) * limit
    rows = await phone_number_repo.list_for_project(
        session, auth.project_id, limit=limit, offset=offset
    )
    total = await phone_number_repo.count_for_project(session, auth.project_id)
    return paginated(
        data=[PhoneNumberResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/{phone_number_id}")
async def get_phone_number(
    phone_number_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get a phone number binding."""
    row = await phone_number_repo.get_by_id(
        session, phone_number_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("PhoneNumber", str(phone_number_id))
    return ok(PhoneNumberResponse.from_row(row))


@router.put("/{phone_number_id}")
async def update_phone_number(
    phone_number_id: UUID,
    body: UpdatePhoneNumberRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Update a binding in place — id and server_url_secret stay stable, so
    call-init endpoints keep verifying with the same secret across edits."""
    row = await phone_number_repo.get_by_id(
        session, phone_number_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("PhoneNumber", str(phone_number_id))

    # Mint a secret only when webhook routing appears for the first time.
    secret = row.server_url_secret
    if body.server_url and not secret:
        secret = secrets_mod.token_urlsafe(32)

    # A new account pair replaces the stored one (rotating a token); omitted
    # keeps it. Verified against the number exactly as on bind.
    own: StoredTwilioAccount | None = None
    if body.has_own_twilio:
        _, own = await _own_twilio(
            body, row.e164_number, row.external_number_sid, get_settings()
        )

    updated = await phone_number_repo.update_phone_number(
        session,
        phone_number_id,
        routing_target_type=body.routing_target_type,
        routing_target_id=body.routing_target_id,
        server_url=body.server_url,
        server_url_secret=secret,
        sms_enabled=body.sms_enabled,
        metadata_json=body.metadata,
        twilio_account=own,
    )
    # Re-run the full Twilio config on every update: it's idempotent, and it's
    # the recovery path after PUBLIC_BASE_URL changes (ngrok rotation) or a
    # bind that happened before PUBLIC_BASE_URL was set.
    configured = await _configure_twilio_webhooks(updated, body.sms_enabled)
    data = PhoneNumberResponse.from_row(updated).model_dump()
    data["twilio_webhooks_configured"] = configured
    return ok(data)


@router.delete("/{phone_number_id}")
async def unbind_phone_number(
    phone_number_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Remove a phone number binding and clear the Twilio number's webhooks —
    an unbound number must not keep pointing Twilio at TurnCall."""
    row = await phone_number_repo.get_by_id(
        session, phone_number_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("PhoneNumber", str(phone_number_id))
    # Resolved before the delete: the number's own account goes with its row.
    account = twilio_accounts.account_for_number(row, get_settings())
    await phone_number_repo.delete_phone_number(session, phone_number_id)

    if account:
        adapter = account.adapter()
        try:
            await adapter.configure_number_webhook(
                row.external_number_sid, voice_url="", status_callback_url=""
            )
            await adapter.configure_sms_webhook(row.external_number_sid, sms_url="")
        except Exception as exc:  # best-effort — the binding is gone either way
            logger.warning(
                "twilio_webhook_clear_failed for {sid}: {err}",
                sid=row.external_number_sid,
                err=exc,
            )
    return ok({"deleted": True})


@router.put("/{phone_number_id}/routing")
async def set_routing_weights(
    phone_number_id: UUID,
    body: SetRoutingWeightsRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Set A/B routing weights on a phone number."""
    row = await phone_number_repo.get_by_id(
        session, phone_number_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("PhoneNumber", str(phone_number_id))

    weights = [{"agent_id": str(w.agent_id), "weight": w.weight} for w in body.weights]
    updated = await phone_number_repo.set_routing_weights(
        session, phone_number_id, weights
    )
    return ok(PhoneNumberResponse.from_row(updated))


@router.get("/{phone_number_id}/routing")
async def get_routing(
    phone_number_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get current routing configuration for a phone number."""
    row = await phone_number_repo.get_by_id(
        session, phone_number_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("PhoneNumber", str(phone_number_id))

    if row.routing_weights:
        return ok(
            RoutingResponse(
                mode="weighted",
                weights=row.routing_weights,
            )
        )
    return ok(
        RoutingResponse(
            mode="single",
            routing_target_id=row.routing_target_id,
        )
    )


@router.delete("/{phone_number_id}/routing")
async def clear_routing_weights(
    phone_number_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Clear A/B routing weights, revert to single-agent routing."""
    row = await phone_number_repo.get_by_id(
        session, phone_number_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("PhoneNumber", str(phone_number_id))

    updated = await phone_number_repo.clear_routing_weights(session, phone_number_id)
    return ok(PhoneNumberResponse.from_row(updated))
