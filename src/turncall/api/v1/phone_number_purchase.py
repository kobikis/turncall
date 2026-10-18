"""Searching and buying numbers on a carrier account (#319, ADR-0025).

Admin-only: buying spends the operator's money. A bought number is bound with
`POST /v1/phone-numbers` like any other; buying writes no binding, so the
audit entry's resource id is the E.164 number.

Twilio has no project credential: its account rides on the number (ADR-0020's
addendum). So the search and the buy run on the Twilio account the request
names (`X-Twilio-Account-Sid` + `X-Twilio-Auth-Token` headers on the search,
which keep the token out of the URL; `twilio_account_sid` +
`twilio_auth_token` in the buy's body, as bind takes them), or on the
platform's TWILIO_* account without one.

This router is mounted before `phone_numbers.router`, whose
`GET /{phone_number_id}` would otherwise take `/available`.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, Query
from pydantic import ValidationError

from turncall.api.audit import Audit
from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ConflictError, ErrorCode
from turncall.api.responses import ok
from turncall.api.v1.schemas.phone_numbers import (
    AvailableNumberResponse,
    BuyPhoneNumberRequest,
    NumberType,
    PurchaseCarrier,
    PurchasedNumberResponse,
    TwilioAccountFields,
)
from turncall.auth import AdminAuth
from turncall.config.settings import get_settings
from turncall.domain.carriers import TWILIO
from turncall.domain.enums import AuditAction, AuditResource
from turncall.services import audit_log, twilio_accounts
from turncall.services.twilio_accounts import TwilioAccount
from turncall.storage.repositories import phone_number_repo

router = APIRouter(prefix="/phone-numbers", tags=["phone-numbers"])


def _unprocessable(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR,
        message=message,
        details=details,
    )


def twilio_account_headers(
    x_twilio_account_sid: Annotated[
        str | None,
        Header(
            description=(
                "A Twilio account to search instead of the platform's; "
                "with X-Twilio-Auth-Token."
            )
        ),
    ] = None,
    x_twilio_auth_token: Annotated[
        str | None, Header(description="That account's Auth Token.")
    ] = None,
) -> TwilioAccountFields:
    """The Twilio account a search names, from its headers: both or neither."""
    try:
        return TwilioAccountFields(
            twilio_account_sid=x_twilio_account_sid,
            twilio_auth_token=x_twilio_auth_token,
        )
    except ValidationError as exc:
        message = "; ".join(e["msg"] for e in exc.errors())
        raise _unprocessable(
            f"X-Twilio-Account-Sid / X-Twilio-Auth-Token: {message}"
        ) from exc


TwilioHeaders = Annotated[TwilioAccountFields, Depends(twilio_account_headers)]


@router.get("/available")
async def search_available_numbers(
    auth: AdminAuth,
    twilio: TwilioHeaders,
    carrier: Annotated[
        PurchaseCarrier, Query(description="The carrier account to search.")
    ] = TWILIO,
    country: Annotated[
        str, Query(pattern=r"^[A-Za-z]{2}$", description="ISO 3166 country code.")
    ] = "US",
    number_type: Annotated[NumberType, Query()] = "local",
    area_code: Annotated[str | None, Query(pattern=r"^[0-9]{2,8}$")] = None,
    contains: Annotated[
        str | None,
        Query(pattern=r"^[0-9]{2,16}$", description="Digits the number contains."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict:
    """Numbers the carrier account can buy, voice-capable. Admin only."""
    if carrier != TWILIO and twilio.has_own_twilio:
        raise _unprocessable(f"X-Twilio-* headers apply to Twilio, not {carrier}")
    found = await _search_on_twilio(
        twilio,
        country.upper(),
        number_type=number_type,
        area_code=area_code,
        contains=contains,
        limit=limit,
    )
    return ok(
        [
            AvailableNumberResponse(carrier=carrier, **number).model_dump()
            for number in found
        ]
    )


@router.post("/purchase", status_code=201)
async def buy_phone_number(
    body: BuyPhoneNumberRequest,
    auth: AdminAuth,
    session: DbSession,
    audit: Audit,
) -> dict:
    """Buy a number onto the carrier account; bind it with
    `POST /v1/phone-numbers`. Admin only. Irreversible on the carrier side:
    the number stays on the account (and is billed) until released."""
    if await phone_number_repo.get_by_e164(session, body.e164_number) is not None:
        raise ConflictError(f"Phone number {body.e164_number} is already bound")

    bought = await _buy_on_twilio(body)
    audit.record(
        AuditResource.PHONE_NUMBER,
        bought.e164_number,
        AuditAction.PURCHASED,
        after=audit_log.fields(
            carrier=bought.carrier,
            e164_number=bought.e164_number,
            external_number_sid=bought.external_number_sid,
            twilio_account_sid=bought.twilio_account_sid,
        ),
    )
    return ok(bought.model_dump())


async def _search_on_twilio(
    fields: TwilioAccountFields, country: str, **filters: Any
) -> list[dict[str, Any]]:
    from twilio.base.exceptions import TwilioRestException

    account = _twilio_account(fields)
    try:
        return list(await account.adapter().search_numbers(country, **filters))
    except TwilioRestException as exc:
        if exc.status == 404:
            raise _unprocessable(
                f"Twilio sells no {filters['number_type']} numbers in {country}"
            ) from exc
        _twilio_failed(exc, own=fields.has_own_twilio)


async def _buy_on_twilio(body: BuyPhoneNumberRequest) -> PurchasedNumberResponse:
    from twilio.base.exceptions import TwilioRestException

    account = _twilio_account(body)
    try:
        number = await account.adapter().buy_number(body.e164_number)
    except TwilioRestException as exc:
        if exc.status == 400:
            # No longer available, or the country needs an address or a
            # regulatory bundle on the account first.
            raise _unprocessable(
                f"Twilio: {exc.msg}", details={"twilio_code": exc.code}
            ) from exc
        _twilio_failed(exc, own=body.has_own_twilio)
    return PurchasedNumberResponse(
        carrier=TWILIO,
        e164_number=number["e164_number"],
        external_number_sid=number["number_sid"],
        friendly_name=number.get("friendly_name"),
        status=number.get("status", "active"),
        twilio_account_sid=account.account_sid if body.has_own_twilio else None,
        twilio_credentials="number" if body.has_own_twilio else "platform",
    )


def _twilio_account(fields: TwilioAccountFields) -> TwilioAccount:
    """The account the request names, else the platform's."""
    if fields.has_own_twilio:
        return TwilioAccount(
            fields.twilio_account_sid or "", fields.twilio_auth_token or ""
        )
    account = twilio_accounts.platform_account(get_settings())
    if account is None:
        raise ApiError(
            status_code=503,
            code=ErrorCode.NO_TWILIO_ACCOUNT,
            message=(
                "No Twilio account: send twilio_account_sid + twilio_auth_token "
                "(X-Twilio-Account-Sid + X-Twilio-Auth-Token on a search), or set "
                "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            ),
        )
    return account


def _twilio_failed(exc: Any, *, own: bool) -> NoReturn:
    """Twilio refused: the caller's own credentials are theirs to fix (422);
    the platform's, or Twilio failing, are not (502)."""
    if own and exc.status in (401, 403):
        raise _unprocessable(
            "Twilio rejected this Account SID / Auth Token pair"
        ) from exc
    raise ApiError(
        status_code=502, code=ErrorCode.TWILIO_ERROR, message=f"Twilio: {exc.msg}"
    ) from exc
