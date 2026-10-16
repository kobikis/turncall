"""The one exit point for events (#312, ADR-0024).

Every row written to a call's event log and every event sent to webhook
subscribers leaves through `_leave`, so redaction (#313) has one place to
stand: it runs there once per event, before the store and before the send.
Nothing else in the engine calls `call_repo.create_call_event` or
`dispatcher.dispatch_event`; `tests/unit/test_event_exit_point.py` fails the
build if something does.

Four entries, by where an event goes:

- `emit_call_event` — stored on the call's log *and* sent. Most call events.
- `record_call_event` — stored only, never sent: the carrier's own
  `call.ended` rows, the agent's hangup marker, `metrics.turn`,
  `delegation.completed`, `voicemail.detected`.
- `dispatch_call_event` — sent only: the comprehensive `call.ended`, already
  stored as its status rows and sent once post-call work is done (ADR-0006).
- `dispatch_project_event` — sent only, with no call behind it: text
  sessions, eval runs, campaigns, and `call.rejected` (refused before a call
  row existed).

The send-only entries let a dispatch failure propagate, as the dispatcher
always has; their callers log it. `emit_call_event` logs and swallows it, so
a dead subscriber never costs the stored row.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

# Looked up through the modules at call time, never imported by name, so a
# test that patches `dispatcher.dispatch_event` or `call_repo.create_call_event`
# patches this exit point too.
from turncall.events import dispatcher
from turncall.storage.repositories import call_repo


async def emit_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    commit: bool = False,
) -> None:
    """Store a call event on the call's log, then send it to subscribers.

    `commit=True` commits the store before sending, for a writer that owns
    its session (the transcript taps, the tool bridge): the row is durable
    before any subscriber hears of it, and the per-call sequence lock is
    released first. Otherwise the caller commits, after the send.
    """
    await _leave(
        session,
        project_id=project_id,
        event_type=event_type,
        payload=payload,
        call_id=call_id,
        store=True,
        commit=commit,
        send=True,
        quiet_send=True,
    )


async def record_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    sequence_number: int | None = None,
) -> None:
    """Store a call event on the call's log and send it nowhere.

    The next sequence number unless one is given. The caller commits.
    """
    await _leave(
        session,
        project_id=project_id,
        event_type=event_type,
        payload=payload,
        call_id=call_id,
        store=True,
        sequence_number=sequence_number,
    )


async def dispatch_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Send a call's event to subscribers without storing it."""
    await _leave(
        session,
        project_id=project_id,
        event_type=event_type,
        payload=payload,
        call_id=call_id,
        send=True,
    )


async def dispatch_project_event(
    session: AsyncSession,
    *,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    session_id: UUID | None = None,
    eval_run_id: UUID | None = None,
    campaign_id: UUID | None = None,
    agent_id: UUID | None = None,
) -> None:
    """Send a project's event that has no call: what it is about rides in
    the envelope (ADR-0007)."""
    await _leave(
        session,
        project_id=project_id,
        event_type=event_type,
        payload=payload,
        send=True,
        session_id=session_id,
        eval_run_id=eval_run_id,
        campaign_id=campaign_id,
        agent_id=agent_id,
    )


async def _leave(
    session: AsyncSession,
    *,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    call_id: UUID | None = None,
    store: bool = False,
    sequence_number: int | None = None,
    commit: bool = False,
    send: bool = False,
    quiet_send: bool = False,
    **envelope: UUID | None,
) -> None:
    """Where every event leaves: stored, then committed, then sent, each as
    asked. #313 redacts `payload` here, before either."""
    if store:
        if call_id is None:
            raise ValueError("only a call's event is stored")
        if sequence_number is None:
            sequence_number = await call_repo.get_next_sequence_number(session, call_id)
        await call_repo.create_call_event(
            session,
            call_id=call_id,
            event_type=event_type,
            payload=payload,
            sequence_number=sequence_number,
        )
    if commit:
        await session.commit()
    if not send:
        return
    # Only what the caller named, as each call site passed it before.
    named = {k: v for k, v in {"call_id": call_id, **envelope}.items() if v is not None}
    try:
        await dispatcher.dispatch_event(
            session,
            project_id=project_id,
            event_type=event_type,
            payload=payload,
            **named,
        )
    except Exception:
        if not quiet_send:
            raise
        logger.exception(
            "event_dispatch_error",
            event_type=event_type,
            call_id=str(call_id),
        )
