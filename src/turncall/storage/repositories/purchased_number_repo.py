"""The numbers a project bought, and on which account (#321, ADR-0025).

A release on the platform's shared carrier account is allowed only for a
number the project bought there through `POST /v1/phone-numbers/purchase`;
this is the record of that. Releases on a project's own account are not
restricted by it.
"""

import hashlib
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.carriers import PLATFORM_ACCOUNT
from turncall.storage.models import PurchasedNumberRow


async def record(
    session: AsyncSession,
    *,
    project_id: UUID,
    carrier: str,
    e164_number: str,
    account: str,
) -> None:
    """Note that the project bought `e164_number` on `account`.

    A number bought on the platform's account was for sale, so it had left
    the account: an earlier project's row for it is stale (a release made
    outside TurnCall) and is dropped, or that project could release the
    number this one now holds. The purchase is real and already billed, so
    it must take ownership, never fail on another project's row: purchases
    of one platform number take a transaction-scoped lock on it first, so a
    racing purchase's row is committed before this one's sweep looks (and is
    swept), rather than landing between the sweep and the insert and
    violating `uq_purchased_numbers_platform_number`."""
    if account == PLATFORM_ACCOUNT:
        await session.execute(
            select(func.pg_advisory_xact_lock(_lock_key(carrier, e164_number)))
        )
        await session.execute(
            delete(PurchasedNumberRow).where(
                PurchasedNumberRow.carrier == carrier,
                PurchasedNumberRow.e164_number == e164_number,
                PurchasedNumberRow.account == PLATFORM_ACCOUNT,
                PurchasedNumberRow.project_id != project_id,
            )
        )
    statement = insert(PurchasedNumberRow).values(
        project_id=project_id,
        carrier=carrier,
        e164_number=e164_number,
        account=account,
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_purchased_numbers_project_number",
            set_={"account": account, "created_at": func.now()},
        )
    )


def _lock_key(carrier: str, e164_number: str) -> int:
    """The advisory lock of one number on the platform's account: a stable
    signed 64-bit key (Python's `hash` is salted per process)."""
    digest = hashlib.sha256(f"purchased_numbers:{carrier}:{e164_number}".encode())
    return int.from_bytes(digest.digest()[:8], "big", signed=True)


async def bought_on_platform(
    session: AsyncSession, *, project_id: UUID, carrier: str, e164_number: str
) -> bool:
    """Whether the project bought `e164_number` on the platform's account."""
    return bool(
        await session.scalar(
            select(
                select(PurchasedNumberRow.id)
                .where(
                    PurchasedNumberRow.project_id == project_id,
                    PurchasedNumberRow.carrier == carrier,
                    PurchasedNumberRow.e164_number == e164_number,
                    PurchasedNumberRow.account == PLATFORM_ACCOUNT,
                )
                .exists()
            )
        )
    )


async def forget(
    session: AsyncSession, *, project_id: UUID, carrier: str, e164_number: str
) -> None:
    """The number was released: no longer the project's."""
    await session.execute(
        delete(PurchasedNumberRow).where(
            PurchasedNumberRow.project_id == project_id,
            PurchasedNumberRow.carrier == carrier,
            PurchasedNumberRow.e164_number == e164_number,
        )
    )


async def bought_on_platform_by(
    session: AsyncSession, project_id: UUID
) -> list[tuple[str, str]]:
    """The (carrier, E.164) of every number the project bought on the
    platform's account and has not released: what a purge leaves on that
    account, still billed, for an operator to release."""
    rows = await session.execute(
        select(PurchasedNumberRow.carrier, PurchasedNumberRow.e164_number)
        .where(
            PurchasedNumberRow.project_id == project_id,
            PurchasedNumberRow.account == PLATFORM_ACCOUNT,
        )
        .order_by(PurchasedNumberRow.carrier, PurchasedNumberRow.e164_number)
    )
    return [(row.carrier, row.e164_number) for row in rows]
