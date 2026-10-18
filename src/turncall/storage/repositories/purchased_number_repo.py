"""The numbers a project bought, and on which account (#321, ADR-0025).

A release on the platform's shared carrier account is allowed only for a
number the project bought there through `POST /v1/phone-numbers/purchase`;
this is the record of that. Releases on a project's own account are not
restricted by it.
"""

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
    number this one now holds."""
    if account == PLATFORM_ACCOUNT:
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
