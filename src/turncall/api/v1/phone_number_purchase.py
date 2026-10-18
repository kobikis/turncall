"""Searching, buying and releasing numbers on a carrier account (#319, #320,
#321, ADR-0025).

Admin-only: buying spends the operator's money and releasing is irreversible.
A bought number is bound with `POST /v1/phone-numbers` like any other; buying
writes no binding, so the audit entry's resource id is the E.164 number.

A number is bound exactly when TurnCall has a `phone_numbers` row for it, so
a release targets a number on the carrier account by its E.164, refuses one
that is bound (409: unbind it first, which leaves it on the account), and
needs `confirm` to repeat the number (422 without it). Unbinding never
releases.

The platform's carrier accounts are shared by every project, so a release
that would run on one (Twilio without an account pair; Telnyx when the
project holds no `telnyx` credential) is allowed only for a number this
project bought there through `POST /v1/phone-numbers/purchase`, which each
purchase records in `purchased_numbers` (403 otherwise, before the carrier is
asked anything). A release on the project's own account is not restricted:
that account is the operator's.

Twilio has no project credential: its account rides on the number (ADR-0020's
addendum). So the search and the buy run on the Twilio account the request
names (`X-Twilio-Account-Sid` + `X-Twilio-Auth-Token` headers on the search,
which keep the token out of the URL; `twilio_account_sid` +
`twilio_auth_token` in the buy's body, as bind takes them), or on the
platform's TWILIO_* account without one.

Telnyx runs on the project's Telnyx account: its `telnyx` credential, else the
platform's TELNYX_API_KEY (ADR-0022), as bind does. A Telnyx purchase is a
number order that finishes asynchronously: the buy waits a few seconds for it
(`TelnyxAdapter.buy_number`) and answers "active" with the number's id, or
"pending" without one. Bind looks a Telnyx number up by its E.164 either way,
so a pending number binds once Telnyx has put it on the account.

This router is mounted before `phone_numbers.router`, whose
`GET /{phone_number_id}` would otherwise take `/available`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Query
from loguru import logger
from pydantic import BaseModel, ValidationError

from turncall.api.audit import Audit
from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ConflictError, ErrorCode
from turncall.api.responses import ok
from turncall.api.v1.carrier_errors import (
    telnyx_failed,
    telnyx_unanswered,
    twilio_failed,
    unprocessable,
)
from turncall.api.v1.phone_numbers import project_telnyx_account
from turncall.api.v1.schemas.phone_numbers import (
    AvailableNumberResponse,
    BuyPhoneNumberRequest,
    NumberType,
    PurchaseCarrier,
    PurchasedNumberResponse,
    ReleasedNumberResponse,
    ReleasePhoneNumberRequest,
    TwilioAccountFields,
)
from turncall.auth import AdminAuth
from turncall.config.settings import get_settings
from turncall.domain.carriers import OWN_ACCOUNT, PLATFORM_ACCOUNT, TELNYX, TWILIO
from turncall.domain.enums import AuditAction, AuditResource
from turncall.services import audit_log, twilio_accounts
from turncall.services.telnyx_accounts import TelnyxAccount
from turncall.services.twilio_accounts import TwilioAccount
from turncall.storage.repositories import phone_number_repo, purchased_number_repo

router = APIRouter(prefix="/phone-numbers", tags=["phone-numbers"])


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
        raise unprocessable(
            f"X-Twilio-Account-Sid / X-Twilio-Auth-Token: {message}"
        ) from exc


TwilioHeaders = Annotated[TwilioAccountFields, Depends(twilio_account_headers)]


@router.get("/available")
async def search_available_numbers(
    auth: AdminAuth,
    twilio: TwilioHeaders,
    session: DbSession,
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
        raise unprocessable(f"X-Twilio-* headers apply to Twilio, not {carrier}")
    filters: dict[str, Any] = {
        "number_type": number_type,
        "area_code": area_code,
        "contains": contains,
        "limit": limit,
    }
    if carrier == TELNYX:
        account = await project_telnyx_account(
            session, auth.project_id, purpose="search for numbers on"
        )
        found = await _search_on_telnyx(account, country.upper(), **filters)
    else:
        found = await _search_on_twilio(twilio, country.upper(), **filters)
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

    if body.carrier == TELNYX:
        telnyx = await project_telnyx_account(
            session, auth.project_id, purpose="buy the number on"
        )
        shared = telnyx.shared
        await _end_reads(session)
        bought = await _buy_on_telnyx(telnyx, body.e164_number, auth.project_id)
        after = audit_log.fields(
            carrier=bought.carrier,
            e164_number=bought.e164_number,
            external_number_sid=bought.external_number_sid,
            status=bought.status,
            telnyx_order_id=bought.telnyx_order_id,
        )
    else:
        twilio = _twilio_account(body)
        shared = twilio.shared
        await _end_reads(session)
        bought = await _buy_on_twilio(body, twilio)
        after = audit_log.fields(
            carrier=bought.carrier,
            e164_number=bought.e164_number,
            external_number_sid=bought.external_number_sid,
            twilio_account_sid=bought.twilio_account_sid,
        )
    async with _tracked(session, "bought", bought, auth.project_id):
        # Recorded for a pending Telnyx order too: the number is the
        # project's once Telnyx finishes it.
        await purchased_number_repo.record(
            session,
            project_id=auth.project_id,
            carrier=bought.carrier,
            e164_number=bought.e164_number,
            account=PLATFORM_ACCOUNT if shared else OWN_ACCOUNT,
        )
        audit.record(
            AuditResource.PHONE_NUMBER,
            bought.e164_number,
            AuditAction.PURCHASED,
            after=after,
        )
    return ok(bought.model_dump())


async def _end_reads(session: Any) -> None:
    """End the request's transaction before the carrier is asked anything,
    so no pooled connection sits idle in a transaction while it answers (a
    Telnyx order is looked at for seconds). Only reads ran in it (the key,
    the binding check, the account; a legacy key's hash upgrade, which is
    then made on the key's next request); what is written after the carrier
    answers starts a new one. A rollback, not a commit: a commit before the
    audit entry is recorded would be refused."""
    await session.rollback()


@asynccontextmanager
async def _tracked(
    session: Any, done: str, number: BaseModel, project_id: UUID
) -> AsyncIterator[None]:
    """Record what the carrier just did, and commit it here rather than
    after the route returns, so a failure is caught: the carrier side is
    done and irreversible (a bought number is billed) whether TurnCall
    records it or not. Failing, the number is logged at error level with
    everything an operator needs to find it, and the client gets a 500 that
    names it too."""
    try:
        yield
        await session.commit()
    except Exception as exc:
        state = number.model_dump(exclude_none=True)
        logger.error(
            "phone_number_{done}_untracked: {carrier} {done} {e164} (number "
            "{sid}, order {order}) for project {project_id}, but TurnCall could "
            "not record it: {error!r}",
            done=done,
            carrier=state.get("carrier"),
            e164=state.get("e164_number"),
            sid=state.get("external_number_sid"),
            order=state.get("telnyx_order_id"),
            project_id=str(project_id),
            error=exc,
            number=state,
        )
        raise ApiError(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message=(
                f"The carrier {done} {state.get('e164_number')}, but TurnCall "
                "could not record it; the number's details are in `details`"
            ),
            details={done: state},
        ) from exc


@router.post("/release")
async def release_phone_number(
    body: ReleasePhoneNumberRequest,
    auth: AdminAuth,
    session: DbSession,
    audit: Audit,
) -> dict:
    """Give a number on the carrier account back to the carrier. Admin only.
    **Irreversible**: the carrier may sell the number to someone else.
    `confirm` must repeat `e164_number` (422 otherwise). A number TurnCall
    still binds is refused (409): unbind it first with
    `DELETE /v1/phone-numbers/{id}`, which leaves it on the account. On the
    platform's shared account (Twilio without an account pair; Telnyx when
    the project holds no `telnyx` credential) only a number this project
    bought through `POST /v1/phone-numbers/purchase` is released (403
    otherwise); the project's own account is not restricted."""
    bound = await phone_number_repo.get_by_e164(session, body.e164_number)
    if bound is not None:
        ours = bound.project_id == auth.project_id
        raise ConflictError(
            f"Phone number {body.e164_number} is bound; unbind it before releasing it",
            details={"phone_number_id": str(bound.id)} if ours else None,
        )

    if body.carrier == TELNYX:
        telnyx = await project_telnyx_account(
            session, auth.project_id, purpose="release the number on"
        )
        await _bought_here_if_shared(session, auth.project_id, body, telnyx.shared)
        await _end_reads(session)
        released = await _release_on_telnyx(telnyx, body.e164_number, auth.project_id)
        before = audit_log.fields(
            carrier=released.carrier,
            e164_number=released.e164_number,
            external_number_sid=released.external_number_sid,
        )
    else:
        twilio = _twilio_account(body)
        await _bought_here_if_shared(session, auth.project_id, body, twilio.shared)
        await _end_reads(session)
        released = await _release_on_twilio(body, twilio)
        before = audit_log.fields(
            carrier=released.carrier,
            e164_number=released.e164_number,
            external_number_sid=released.external_number_sid,
            twilio_account_sid=released.twilio_account_sid,
        )
    async with _tracked(session, "released", released, auth.project_id):
        await purchased_number_repo.forget(
            session,
            project_id=auth.project_id,
            carrier=released.carrier,
            e164_number=released.e164_number,
        )
        audit.record(
            AuditResource.PHONE_NUMBER,
            released.e164_number,
            AuditAction.RELEASED,
            before=before,
        )
    return ok(released.model_dump())


async def _bought_here_if_shared(
    session: Any,
    project_id: Any,
    body: ReleasePhoneNumberRequest,
    shared: bool,
) -> None:
    """Refuse a release on a shared account (the platform's) of a number this
    project did not buy there (403), before the carrier is asked anything:
    the account holds every project's numbers, and the operator's own. The
    refusal does not say whether the account holds the number."""
    if not shared:
        return
    if await purchased_number_repo.bought_on_platform(
        session,
        project_id=project_id,
        carrier=body.carrier,
        e164_number=body.e164_number,
    ):
        return
    name = "Twilio" if body.carrier == TWILIO else "Telnyx"
    own = (
        "send twilio_account_sid + twilio_auth_token"
        if body.carrier == TWILIO
        else "store the project's `telnyx` credential"
    )
    raise ApiError(
        status_code=403,
        code=ErrorCode.FORBIDDEN,
        message=(
            f"This project did not buy {body.e164_number} on the platform's "
            f"{name} account through POST /v1/phone-numbers/purchase, so it "
            f"cannot release it there; to release a number on your own {name} "
            f"account, {own}"
        ),
        details={"account": PLATFORM_ACCOUNT},
    )


async def _search_on_twilio(
    fields: TwilioAccountFields, country: str, **filters: Any
) -> list[dict[str, Any]]:
    from twilio.base.exceptions import TwilioRestException

    account = _twilio_account(fields)
    try:
        return list(await account.adapter().search_numbers(country, **filters))
    except TwilioRestException as exc:
        if exc.status == 404:
            raise unprocessable(
                f"Twilio sells no {filters['number_type']} numbers in {country}"
            ) from exc
        twilio_failed(exc, own=fields.has_own_twilio)


async def _buy_on_twilio(
    body: BuyPhoneNumberRequest, account: TwilioAccount
) -> PurchasedNumberResponse:
    from twilio.base.exceptions import TwilioRestException

    try:
        number = await account.adapter().buy_number(body.e164_number)
    except TwilioRestException as exc:
        if exc.status == 400:
            # No longer available, or the country needs an address or a
            # regulatory bundle on the account first.
            raise unprocessable(
                f"Twilio: {exc.msg}", details={"twilio_code": exc.code}
            ) from exc
        twilio_failed(exc, own=body.has_own_twilio)
    return PurchasedNumberResponse(
        carrier=TWILIO,
        e164_number=number["e164_number"],
        external_number_sid=number["number_sid"],
        friendly_name=number.get("friendly_name"),
        status=number.get("status", "active"),
        twilio_account_sid=account.account_sid if body.has_own_twilio else None,
        twilio_credentials="number" if body.has_own_twilio else "platform",
    )


async def _release_on_twilio(
    body: ReleasePhoneNumberRequest, account: TwilioAccount
) -> ReleasedNumberResponse:
    from twilio.base.exceptions import TwilioRestException

    adapter = account.adapter()
    try:
        number_sid = await adapter.find_number_sid(body.e164_number)
        if number_sid is None:
            raise unprocessable(
                f"{body.e164_number} is not a phone number on this Twilio account"
            )
        await adapter.release_number(number_sid)
    except TwilioRestException as exc:
        if exc.status == 404:
            # Gone between the lookup and the release: not released here.
            raise unprocessable(
                f"{body.e164_number} is not a phone number on this Twilio account"
            ) from exc
        twilio_failed(exc, own=body.has_own_twilio)
    return ReleasedNumberResponse(
        carrier=TWILIO,
        e164_number=body.e164_number,
        external_number_sid=number_sid,
        twilio_account_sid=account.account_sid if body.has_own_twilio else None,
        twilio_credentials="number" if body.has_own_twilio else "platform",
    )


def _twilio_account(fields: TwilioAccountFields) -> TwilioAccount:
    """The account the request names, else the platform's."""
    if fields.has_own_twilio:
        return TwilioAccount(
            fields.twilio_account_sid or "",
            fields.twilio_auth_token or "",
            shared=False,
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


async def _search_on_telnyx(
    account: TelnyxAccount, country: str, **filters: Any
) -> list[dict[str, Any]]:
    from turncall.adapters.telephony.telnyx_adapter import TelnyxApiError

    try:
        return list(await account.adapter().search_numbers(country, **filters))
    except TelnyxApiError as exc:
        telnyx_failed(exc, request_is_callers=True)
    except httpx.HTTPError as exc:
        telnyx_unanswered(exc)


async def _buy_on_telnyx(
    account: TelnyxAccount, e164: str, project_id: UUID
) -> PurchasedNumberResponse:
    from turncall.adapters.telephony.telnyx_adapter import (
        TelnyxApiError,
        TelnyxOrderFailed,
    )

    try:
        number = await account.adapter().buy_number(e164)
    except TelnyxOrderFailed as exc:
        raise unprocessable(
            f"Telnyx could not complete the number order for {e164}",
            details={"telnyx_order_id": exc.order_id},
        ) from exc
    except TelnyxApiError as exc:
        telnyx_failed(exc, request_is_callers=True)
    except httpx.HTTPError as exc:
        # Only the order request itself raises this (its looks afterwards are
        # quiet): Telnyx may have taken the order and the answer been lost.
        logger.error(
            "telnyx_number_order_unanswered: Telnyx did not answer the number "
            "order for {e164} (project {project_id}): {error!r}. The order may "
            "have been placed: check the account's number orders.",
            e164=e164,
            project_id=str(project_id),
            error=exc,
            shared_account=account.shared,
        )
        telnyx_unanswered(
            exc,
            f"the number order for {e164} may have been placed; check the "
            "Telnyx account's number orders before buying it again",
        )
    return PurchasedNumberResponse(
        carrier=TELNYX,
        e164_number=number["e164_number"],
        external_number_sid=number["number_sid"],
        friendly_name=number.get("friendly_name"),
        status=number["status"],
        telnyx_order_id=number.get("order_id"),
    )


async def _release_on_telnyx(
    account: TelnyxAccount, e164: str, project_id: UUID
) -> ReleasedNumberResponse:
    from turncall.adapters.telephony.telnyx_adapter import TelnyxApiError

    adapter = account.adapter()
    try:
        found = await adapter.find_number(e164)
    except TelnyxApiError as exc:
        telnyx_failed(exc, request_is_callers=True)
    except httpx.HTTPError as exc:
        telnyx_unanswered(exc)
    if found is None:
        # Also a number whose order has not finished: it is not on the
        # account yet.
        raise unprocessable(
            f"{e164} is not a phone number on this project's Telnyx account"
        )
    number_id = str(found["id"])
    try:
        await adapter.release_number(number_id)
    except TelnyxApiError as exc:
        telnyx_failed(exc, request_is_callers=True)
    except httpx.HTTPError as exc:
        logger.error(
            "telnyx_number_release_unanswered: Telnyx did not answer the "
            "release of {e164} (number {number_id}, project {project_id}): "
            "{error!r}. It may have been released.",
            e164=e164,
            number_id=number_id,
            project_id=str(project_id),
            error=exc,
        )
        telnyx_unanswered(
            exc,
            f"{e164} may have been released; check the Telnyx account before "
            "releasing it again",
        )
    return ReleasedNumberResponse(
        carrier=TELNYX, e164_number=e164, external_number_sid=number_id
    )
