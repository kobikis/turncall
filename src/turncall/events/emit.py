"""Emit call events: create DB record + dispatch to webhook subscribers.

Single helper that replaces the two-step pattern of
call_repo.create_call_event() + dispatcher.dispatch_event().
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.events.dispatcher import dispatch_event
from turncall.storage.repositories import call_repo


async def emit_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Create a call event in the DB and dispatch to webhook subscribers."""
    seq = await call_repo.get_next_sequence_number(session, call_id)
    await call_repo.create_call_event(
        session,
        call_id=call_id,
        event_type=event_type,
        payload=payload,
        sequence_number=seq,
    )

    try:
        await dispatch_event(
            session,
            project_id=project_id,
            event_type=event_type,
            payload=payload,
            call_id=call_id,
        )
    except Exception:
        logger.exception(
            "event_dispatch_error",
            event_type=event_type,
            call_id=str(call_id),
        )
