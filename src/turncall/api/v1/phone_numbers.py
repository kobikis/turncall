"""Phone number management endpoints."""

import secrets as secrets_mod
from uuid import UUID

from fastapi import APIRouter
from loguru import logger

from turncall.api.deps import DbSession
from turncall.api.errors import ConflictError, NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.phone_numbers import (
    BindPhoneNumberRequest,
    PhoneNumberResponse,
    RoutingResponse,
    SetRoutingWeightsRequest,
    UpdatePhoneNumberRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.config.settings import get_settings
from turncall.storage.repositories import phone_number_repo

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

    row = await phone_number_repo.bind_phone_number(
        session,
        project_id=auth.project_id,
        external_number_sid=body.external_number_sid,
        e164_number=body.e164_number,
        routing_target_type=body.routing_target_type,
        routing_target_id=body.routing_target_id,
        server_url=body.server_url,
        server_url_secret=server_url_secret,
        sms_enabled=body.sms_enabled,
        metadata_json=body.metadata,
    )

    configured = await _configure_twilio_webhooks(
        body.external_number_sid, body.sms_enabled
    )

    data = PhoneNumberResponse.from_row(row).model_dump()
    # False = TurnCall did NOT point the Twilio number at itself (missing
    # PUBLIC_BASE_URL/credentials or a Twilio error) — the number won't receive
    # calls until its Voice webhook is set. Surfaced so clients can warn.
    data["twilio_webhooks_configured"] = configured
    return ok(data)


async def _configure_twilio_webhooks(
    external_number_sid: str, sms_enabled: bool
) -> bool:
    """Point the Twilio number's voice + status (and SMS, if enabled) webhooks at
    TurnCall, so inbound calls reach us and finalize on hangup. Needs PUBLIC_BASE_URL
    (Twilio must reach us) + Twilio credentials. Best-effort: a Twilio failure logs
    a warning but does not fail the bind — the routing row is already recorded.
    Returns True iff Twilio was actually configured."""
    settings = get_settings()
    base = (settings.server.public_base_url or "").rstrip("/")
    if not (base and settings.twilio.account_sid and settings.twilio.auth_token):
        logger.warning(
            "twilio_webhook_not_configured: set PUBLIC_BASE_URL + Twilio creds "
            "(number {sid} left unconfigured — set its Voice webhook manually)",
            sid=external_number_sid,
        )
        return False

    from turncall.adapters.telephony.twilio_adapter import TwilioAdapter

    adapter = TwilioAdapter(settings.twilio.account_sid, settings.twilio.auth_token)
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

    updated = await phone_number_repo.update_phone_number(
        session,
        phone_number_id,
        routing_target_type=body.routing_target_type,
        routing_target_id=body.routing_target_id,
        server_url=body.server_url,
        server_url_secret=secret,
        sms_enabled=body.sms_enabled,
        metadata_json=body.metadata,
    )
    # Re-run the full Twilio config on every update: it's idempotent, and it's
    # the recovery path after PUBLIC_BASE_URL changes (ngrok rotation) or a
    # bind that happened before PUBLIC_BASE_URL was set.
    configured = await _configure_twilio_webhooks(
        row.external_number_sid, body.sms_enabled
    )
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
    await phone_number_repo.delete_phone_number(session, phone_number_id)

    settings = get_settings()
    if settings.twilio.account_sid and settings.twilio.auth_token:
        from turncall.adapters.telephony.twilio_adapter import TwilioAdapter

        adapter = TwilioAdapter(settings.twilio.account_sid, settings.twilio.auth_token)
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
