"""Which Twilio account a number, a call, or a webhook belongs to.

A phone number can carry its own Twilio account — the Account SID in the clear
(it is an identifier, and webhooks are matched by it) and the Auth Token
encrypted under CREDENTIAL_ENCRYPTION_KEY, like a project credential
(ADR-0020). A number without one runs on the platform's TWILIO_* account,
exactly as before.

Every Twilio REST call and every signature check goes through here, so a call
on a customer's number is hung up, transferred and recorded with that
customer's credentials, and a webhook from their account is verified with
their token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from turncall.storage.models import PhoneNumberRow
from turncall.storage.repositories import credential_repo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.config.settings import Settings


@dataclass(frozen=True)
class TwilioAccount:
    account_sid: str
    auth_token: str

    def adapter(self) -> Any:
        from turncall.adapters.telephony.twilio_adapter import TwilioAdapter

        return TwilioAdapter(self.account_sid, self.auth_token)


def platform_account(settings: Settings) -> TwilioAccount | None:
    t = settings.twilio
    return (
        TwilioAccount(t.account_sid, t.auth_token)
        if t.account_sid and t.auth_token
        else None
    )


def encrypt_token(auth_token: str, settings: Settings) -> str:
    return credential_repo.encrypt({"auth_token": auth_token}, settings)


def account_for_number(row: Any, settings: Settings) -> TwilioAccount | None:
    """The number's own account if it has one, else the platform's (or None)."""
    if row is not None and row.twilio_account_sid and row.twilio_auth_token_ciphertext:
        token = credential_repo.decrypt(row.twilio_auth_token_ciphertext, settings)[
            "auth_token"
        ]
        return TwilioAccount(row.twilio_account_sid, token)
    return platform_account(settings)


async def account_for_call(
    session: AsyncSession, call: Any, settings: Settings
) -> TwilioAccount | None:
    """The account of the TurnCall number on this call: the dialled number on an
    inbound call, the caller-id number on an outbound one."""
    from turncall.storage.repositories import phone_number_repo

    e164 = call.from_number if call.direction == "outbound" else call.to_number
    row = await phone_number_repo.get_by_e164(session, e164) if e164 else None
    return account_for_number(row, settings)


async def token_for_account_sid(
    session: AsyncSession, account_sid: str | None, settings: Settings
) -> str | None:
    """The Auth Token that signs webhooks from this account.

    Twilio puts `AccountSid` in every webhook body. A number's own account is
    matched by it; anything else is checked against the platform token — so a
    forged AccountSid naming no stored account still has to pass the platform's
    signature. None when no token is known at all (dev mode, as before).
    """
    if account_sid and account_sid != settings.twilio.account_sid:
        ciphertext = await session.scalar(
            select(PhoneNumberRow.twilio_auth_token_ciphertext)
            .where(PhoneNumberRow.twilio_account_sid == account_sid)
            .where(PhoneNumberRow.twilio_auth_token_ciphertext.is_not(None))
            .limit(1)
        )
        if ciphertext:
            return str(credential_repo.decrypt(ciphertext, settings)["auth_token"])
    return settings.twilio.auth_token or None


async def any_number_has_own_account(session: AsyncSession) -> bool:
    """Whether Twilio is configured per number anywhere on this deployment.

    Decides what an unverifiable webhook means: with no Twilio token at all it
    is a dev setup and passes, as it always did; once numbers carry their own
    accounts, a webhook nothing here can verify is refused.
    """
    found = await session.scalar(
        select(PhoneNumberRow.id)
        .where(PhoneNumberRow.twilio_auth_token_ciphertext.is_not(None))
        .limit(1)
    )
    return found is not None
