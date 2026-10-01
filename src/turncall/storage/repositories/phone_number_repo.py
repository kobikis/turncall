"""Phone number repository - data access for phone number bindings."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import PhoneNumberRow


@dataclass(frozen=True)
class StoredTwilioAccount:
    """A number's own Twilio account as stored: the SID in the clear, the token
    sealed under CREDENTIAL_ENCRYPTION_KEY."""

    account_sid: str
    token_ciphertext: str


async def bind_phone_number(
    session: AsyncSession,
    *,
    project_id: UUID,
    external_number_sid: str,
    e164_number: str,
    routing_target_type: str,
    routing_target_id: UUID | None = None,
    server_url: str | None = None,
    server_url_secret: str | None = None,
    sms_enabled: bool = False,
    metadata_json: dict | None = None,
    twilio_account_sid: str | None = None,
    twilio_auth_token_ciphertext: str | None = None,
) -> PhoneNumberRow:
    """Bind a Twilio phone number to a routing target."""
    row = PhoneNumberRow(
        project_id=project_id,
        external_number_sid=external_number_sid,
        e164_number=e164_number,
        routing_target_type=routing_target_type,
        routing_target_id=routing_target_id,
        server_url=server_url,
        server_url_secret=server_url_secret,
        sms_enabled=sms_enabled,
        metadata_json=metadata_json or {},
        twilio_account_sid=twilio_account_sid,
        twilio_auth_token_ciphertext=twilio_auth_token_ciphertext,
    )
    session.add(row)
    await session.flush()
    return row


async def get_by_e164(
    session: AsyncSession,
    e164_number: str,
) -> PhoneNumberRow | None:
    """Look up a phone number by E.164 number (for inbound routing)."""
    result = await session.execute(
        select(PhoneNumberRow).where(PhoneNumberRow.e164_number == e164_number)
    )
    return result.scalar_one_or_none()


async def get_by_id(
    session: AsyncSession,
    phone_number_id: UUID,
    *,
    project_id: UUID | None = None,
) -> PhoneNumberRow | None:
    """Get a phone number binding by ID."""
    query = select(PhoneNumberRow).where(PhoneNumberRow.id == phone_number_id)
    if project_id is not None:
        query = query.where(PhoneNumberRow.project_id == project_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def list_for_project(
    session: AsyncSession,
    project_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[PhoneNumberRow]:
    """List phone numbers for a project."""
    result = await session.execute(
        select(PhoneNumberRow)
        .where(PhoneNumberRow.project_id == project_id)
        .order_by(PhoneNumberRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def count_for_project(session: AsyncSession, project_id: UUID) -> int:
    """Count phone numbers for a project."""
    result = await session.execute(
        select(func.count(PhoneNumberRow.id)).where(
            PhoneNumberRow.project_id == project_id
        )
    )
    return result.scalar_one()


async def delete_phone_number(
    session: AsyncSession,
    phone_number_id: UUID,
) -> bool:
    """Delete a phone number binding."""
    row = await get_by_id(session, phone_number_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def update_phone_number(
    session: AsyncSession,
    phone_number_id: UUID,
    *,
    routing_target_type: str,
    routing_target_id: UUID | None,
    server_url: str | None,
    server_url_secret: str | None,
    sms_enabled: bool,
    metadata_json: dict | None = None,
    twilio_account: StoredTwilioAccount | None = None,
) -> PhoneNumberRow | None:
    """Update a binding in place — same row id, secret only as supplied.
    `twilio_account` replaces the number's own account; None keeps the stored."""
    values: dict = {
        "routing_target_type": routing_target_type,
        "routing_target_id": routing_target_id,
        "server_url": server_url,
        "server_url_secret": server_url_secret,
        "sms_enabled": sms_enabled,
    }
    if metadata_json is not None:
        values["metadata_json"] = metadata_json
    if twilio_account is not None:
        values["twilio_account_sid"] = twilio_account.account_sid
        values["twilio_auth_token_ciphertext"] = twilio_account.token_ciphertext
    result = await session.execute(
        update(PhoneNumberRow)
        .where(PhoneNumberRow.id == phone_number_id)
        .values(**values)
        .returning(PhoneNumberRow)
    )
    return result.scalar_one_or_none()


async def set_routing_weights(
    session: AsyncSession,
    phone_number_id: UUID,
    weights: list[dict],
) -> PhoneNumberRow | None:
    """Set A/B routing weights on a phone number."""
    result = await session.execute(
        update(PhoneNumberRow)
        .where(PhoneNumberRow.id == phone_number_id)
        .values(routing_weights=weights)
        .returning(PhoneNumberRow)
    )
    return result.scalar_one_or_none()


async def clear_routing_weights(
    session: AsyncSession,
    phone_number_id: UUID,
) -> PhoneNumberRow | None:
    """Clear A/B routing weights, reverting to single-agent routing."""
    result = await session.execute(
        update(PhoneNumberRow)
        .where(PhoneNumberRow.id == phone_number_id)
        .values(routing_weights=None)
        .returning(PhoneNumberRow)
    )
    return result.scalar_one_or_none()


async def token_ciphertext_for_account(
    session: AsyncSession, account_sid: str
) -> str | None:
    """The sealed token of any number carrying this Twilio account, or None."""
    return await session.scalar(
        select(PhoneNumberRow.twilio_auth_token_ciphertext)
        .where(PhoneNumberRow.twilio_account_sid == account_sid)
        .where(PhoneNumberRow.twilio_auth_token_ciphertext.is_not(None))
        .limit(1)
    )


async def any_has_own_twilio_account(session: AsyncSession) -> bool:
    """Whether any number on this deployment carries its own Twilio account."""
    found = await session.scalar(
        select(PhoneNumberRow.id)
        .where(PhoneNumberRow.twilio_auth_token_ciphertext.is_not(None))
        .limit(1)
    )
    return found is not None
