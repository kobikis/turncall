"""The one exit point for call events and webhooks (#312, ADR-0024).

What it covers: every row written to a call's event log (`call_events`) and
every event sent to webhook subscribers (`dispatcher.dispatch_event`) goes
through here, and nothing else in the engine calls
`call_repo.create_call_event` or `dispatcher.dispatch_event`.
`tests/unit/test_event_exit_point.py` fails the build if something does.
Redaction (#313) runs here once per event, in `_redact`, before the store and
before the send.

What it does not cover, on purpose:

- `events/server_events.py` (call-init and the other server events) and tool
  webhooks (`services/tool_webhook.py`). They are part of the live call: the
  operator's own server answers them and needs the raw values, and ADR-0024
  never redacts what the live call uses.
- Tool-invocation rows (`tool_invocation_repo.create_invocation`, written by
  `orchestrator/tool_bridge.py` and `services/chat_tools.py`). Those are
  stored data, but not call events; their redaction is #314's.
- Everything else that is stored or logged: the call row (`analysis_json`),
  SMS/chat message rows, logs and spans.

Four entries, by where an event goes:

- `emit_call_event`: stored on the call's log *and* sent. Most call events.
- `record_call_event`: stored only, never sent: the carrier's own
  `call.ended` rows (Twilio's `completed`/`busy`/`no-answer` status
  callback, Telnyx's hangup), the agent's hangup marker (`call.ended`, source
  `control`), `metrics.turn`, `delegation.completed` and `voicemail.detected`.
- `dispatch_call_event`: sent only: the comprehensive `call.ended`, whose
  facts are already on the log, sent once post-call work is done (ADR-0006).
- `dispatch_project_event`: sent only, with no call behind it: text
  sessions, eval runs, campaigns, and `call.rejected` (refused before a call
  row existed).

Each entry is the same three steps, built from the primitives below:
`_redact` the payload, `_store` it, `_send` it. A send runs its database
reads (the subscriber list, the call's agent) inside a SAVEPOINT, so a failed
send cannot abort the caller's transaction and take the stored row with it.
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
    payload = await _redact(session, project_id, payload)
    await _store(session, call_id, event_type, payload, commit=commit)
    try:
        await _send(session, project_id, event_type, payload, call_id=call_id)
    except Exception:
        logger.exception(
            "event_dispatch_error", event_type=event_type, call_id=str(call_id)
        )


async def record_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    commit: bool = False,
) -> None:
    """Store a call event on the call's log and send it nowhere.

    `commit=True` commits it, for a writer that owns its session; otherwise
    the caller commits.
    """
    payload = await _redact(session, project_id, payload)
    await _store(session, call_id, event_type, payload, commit=commit)


async def dispatch_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Send a call's event to subscribers without storing it."""
    payload = await _redact(session, project_id, payload)
    await _send(session, project_id, event_type, payload, call_id=call_id)


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
    payload = await _redact(session, project_id, payload)
    await _send(
        session,
        project_id,
        event_type,
        payload,
        session_id=session_id,
        eval_run_id=eval_run_id,
        campaign_id=campaign_id,
        agent_id=agent_id,
    )


async def _redact(
    session: AsyncSession, project_id: UUID, payload: dict[str, Any]
) -> dict[str, Any]:
    """What of `payload` may leave the call. Every entry above calls this
    first, and passes only what it returns to `_store` and `_send`.

    Nothing is redacted yet: #313 fills this in (the project's redaction
    settings, looked up on `session`).
    """
    return payload


async def _store(
    session: AsyncSession,
    call_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    commit: bool,
) -> None:
    """Append the event to the call's log under the next sequence number,
    committing if asked."""
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


async def _send(
    session: AsyncSession,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    call_id: UUID | None = None,
    session_id: UUID | None = None,
    eval_run_id: UUID | None = None,
    campaign_id: UUID | None = None,
    agent_id: UUID | None = None,
) -> None:
    """Hand the event to the dispatcher, which reads its subscribers and
    queues delivery in the background.

    The dispatcher reads on the caller's session. In a SAVEPOINT, a failed
    read rolls back to it and the caller's transaction (an uncommitted stored
    row, say) is still usable; without one, Postgres aborts the whole
    transaction and the caller's commit fails.
    """
    # Only the envelope fields this event has, as each caller passed them.
    envelope = {
        name: value
        for name, value in {
            "call_id": call_id,
            "session_id": session_id,
            "eval_run_id": eval_run_id,
            "campaign_id": campaign_id,
            "agent_id": agent_id,
        }.items()
        if value is not None
    }
    async with session.begin_nested():
        await dispatcher.dispatch_event(
            session,
            project_id=project_id,
            event_type=event_type,
            payload=payload,
            **envelope,
        )
