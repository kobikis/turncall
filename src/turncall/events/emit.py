"""The one exit point for call events and webhooks (#312, ADR-0024).

What it covers: every row written to a call's event log (`call_events`) and
every event sent to webhook subscribers (`dispatcher.dispatch_event`) goes
through here, and nothing else in the engine calls
`call_repo.create_call_event` or `dispatcher.dispatch_event`.
`tests/unit/test_event_exit_point.py` fails the build if something does.
Redaction (#313) is applied here once per event, before the store and before
the send: a project that turned it on has its chosen PII replaced in every
payload, so what is stored and what subscribers get agree.

What it does not cover, on purpose:

- `events/server_events.py` (call-init and the other server events) and tool
  webhooks (`services/tool_webhook.py`). They are part of the live call: the
  operator's own server answers them and needs the raw values, and ADR-0024
  never redacts what the live call uses.
- Tool-invocation rows (`tool_invocation_repo.create_invocation`, written by
  `orchestrator/tool_bridge.py` and `services/chat_tools.py`). Those are
  stored data, but not call events; their redaction is #314's.
- Everything else that is stored or logged: SMS/chat message rows, logs and
  spans. (The call row's `analysis_json` is redacted where it is written,
  `services/call_analysis_trigger`.)

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

**Redaction never waits inside a transaction.** The entries are called with
the caller's session, often mid-transaction: the call row just written, the
per-call sequence lock (`_store`) taken by an earlier event. Waiting there on
the detector would hold that connection and that lock for as long as the
redaction worker's queue is, stalling every other writer of the call. So the
entries never wait on it. They take either

- a `Redacted` payload, from `redact()`, which the caller awaits *before* its
  transaction (or after ending a read-only one: `redact(..., release=session)`)
  and is the only way text gets through; or
- a plain dict of ids, numbers and structure only (`call.initializing`, a
  status callback's event), which the entry redacts synchronously: phone
  fields replaced, no detector.

Text in a plain dict is a bug at the call site. With `STRICT` (the test suite
sets it) it raises, whatever the project's policy, so a test of the path
fails; in production a redacting project's text there is withheld
(`<REDACTED>`) and logged, never sent raw and never waited on.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

# Looked up through the modules at call time, never imported by name, so a
# test that patches `dispatcher.dispatch_event` or `call_repo.create_call_event`
# patches this exit point too.
from turncall.events import dispatcher
from turncall.services import redaction
from turncall.storage.repositories import call_repo

# Set by the test suite: a plain-dict payload carrying text raises.
STRICT = False


class TextNotRedacted(TypeError):
    """Text reached an entry in a plain dict instead of through `redact()`."""


@dataclass(frozen=True)
class Redacted:
    """A payload as it may leave the call: through its project's redaction,
    for one event type. Built only by `redact()` (the guard test holds that)."""

    event_type: str
    payload: dict[str, Any]

    def plus(self, **values: bool | int | float | None) -> Redacted:
        """With more fields that cannot carry text (a flag, a count), known only
        after the text was redacted: `success` once the carrier answered."""
        for key, value in values.items():
            if value is not None and not isinstance(value, (bool, int, float)):
                raise TextNotRedacted(f"{key} is not a flag or a number")
        return Redacted(self.event_type, {**self.payload, **values})


async def redact(
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    stored: Iterable[str] = (),
    release: AsyncSession | None = None,
    sessions: Callable[[], AsyncSession] | None = None,
) -> Redacted:
    """`payload` through the project's redaction, ready for an entry.

    The one step that may wait on the detector, so call it holding nothing:
    before the session's transaction begins. `release`: a session whose
    transaction holds only reads (everything it wrote is committed), ended
    here first so the wait holds no connection. `stored`: top-level keys whose
    values were read back from this project's stored events, already
    redacted, and passed through rather than analysed again. The policy is
    read on `release` before it is ended, else without the caller's session
    (`redaction.policy_for_project`), on one from `sessions` (the app's
    factory by default).
    """
    event_type = str(event_type)
    if release is not None:
        # The policy read is no wait: on the session, before it is released.
        policy = await redaction.policy_for_project(project_id, session=release)
        if release.in_transaction():
            await release.commit()
    else:
        policy = await redaction.policy_for_project(project_id, sessions=sessions)
    if policy is None:
        return Redacted(event_type, payload)
    return Redacted(
        event_type,
        await redaction.redact_payload(
            payload, policy, event_type=event_type, keep=stored
        ),
    )


async def emit_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any] | Redacted,
    commit: bool = False,
) -> None:
    """Store a call event on the call's log, then send it to subscribers.

    `commit=True` commits the store before sending, for a writer that owns
    its session (the transcript taps, the tool bridge): the row is durable
    before any subscriber hears of it, and the per-call sequence lock is
    released first. Otherwise the caller commits, after the send.
    """
    out = await _redact(session, project_id, event_type, payload)
    await _store(session, call_id, event_type, out, commit=commit)
    try:
        await _send(session, project_id, event_type, out, call_id=call_id)
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
    payload: dict[str, Any] | Redacted,
    commit: bool = False,
) -> None:
    """Store a call event on the call's log and send it nowhere.

    `commit=True` commits it, for a writer that owns its session; otherwise
    the caller commits.
    """
    out = await _redact(session, project_id, event_type, payload)
    await _store(session, call_id, event_type, out, commit=commit)


async def dispatch_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any] | Redacted,
) -> None:
    """Send a call's event to subscribers without storing it."""
    out = await _redact(session, project_id, event_type, payload)
    await _send(session, project_id, event_type, out, call_id=call_id)


async def dispatch_project_event(
    session: AsyncSession,
    *,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any] | Redacted,
    session_id: UUID | None = None,
    eval_run_id: UUID | None = None,
    campaign_id: UUID | None = None,
    agent_id: UUID | None = None,
) -> None:
    """Send a project's event that has no call: what it is about rides in
    the envelope (ADR-0007)."""
    out = await _redact(session, project_id, event_type, payload)
    await _send(
        session,
        project_id,
        event_type,
        out,
        session_id=session_id,
        eval_run_id=eval_run_id,
        campaign_id=campaign_id,
        agent_id=agent_id,
    )


async def _redact(
    session: AsyncSession,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any] | Redacted,
) -> dict[str, Any]:
    """What of `payload` may leave the call. Every entry above calls this
    first, and passes only what it returns to `_store` and `_send`. Never
    waits on the detector (see the module docstring).

    A `Redacted` payload went through `redact()` already. A plain dict holds
    no text (or `STRICT` raises): the project's policy, cached or read on
    `session`, replaces its phone fields, synchronously.
    """
    event_type = str(event_type)
    if isinstance(payload, Redacted):
        if payload.event_type != event_type:
            raise ValueError(
                f"payload redacted for {payload.event_type}, emitted as {event_type}"
            )
        return payload.payload
    text = redaction.texts_in(payload, event_type=event_type)
    if text and STRICT:
        raise TextNotRedacted(
            f"{event_type} carries text; pass it through emit.redact() "
            "before the transaction"
        )
    policy = await redaction.policy_for_project(project_id, session=session)
    if policy is None:
        return payload
    if text:
        logger.error(
            "redaction_text_not_prepared_withheld",
            event_type=event_type,
            strings=len(text),
        )
    return redaction.redact_fields(payload, policy, event_type=event_type)


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
