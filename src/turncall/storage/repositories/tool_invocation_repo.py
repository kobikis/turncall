"""Tool invocation repository."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import ToolInvocationRow


async def create_invocation(
    session: AsyncSession,
    *,
    call_id: UUID,
    tool_name: str,
    input_json: dict[str, Any],
    status: str = "pending",
    idempotency_key: str | None = None,
) -> ToolInvocationRow:
    """Record a tool invocation."""
    row = ToolInvocationRow(
        call_id=call_id,
        tool_name=tool_name,
        input_json=input_json,
        status=status,
        idempotency_key=idempotency_key,
    )
    session.add(row)
    await session.flush()
    return row


async def update_invocation(
    session: AsyncSession,
    invocation_id: UUID,
    *,
    status: str,
    output_json: dict[str, Any] | None = None,
    latency_ms: int | None = None,
) -> ToolInvocationRow | None:
    """Update a tool invocation result."""
    from sqlalchemy import update

    values: dict[str, Any] = {"status": status}
    if output_json is not None:
        values["output_json"] = output_json
    if latency_ms is not None:
        values["latency_ms"] = latency_ms

    result = await session.execute(
        update(ToolInvocationRow)
        .where(ToolInvocationRow.id == invocation_id)
        .values(**values)
        .returning(ToolInvocationRow)
    )
    return result.scalar_one_or_none()


async def list_invocations_for_call(
    session: AsyncSession,
    call_id: UUID,
) -> list[ToolInvocationRow]:
    """List all tool invocations for a call."""
    result = await session.execute(
        select(ToolInvocationRow)
        .where(ToolInvocationRow.call_id == call_id)
        .order_by(ToolInvocationRow.created_at.asc())
    )
    return list(result.scalars().all())


async def get_by_idempotency_key(
    session: AsyncSession,
    idempotency_key: str,
) -> ToolInvocationRow | None:
    """Look up an invocation by idempotency key (dedup)."""
    result = await session.execute(
        select(ToolInvocationRow).where(
            ToolInvocationRow.idempotency_key == idempotency_key
        )
    )
    return result.scalar_one_or_none()
