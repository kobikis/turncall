"""Redaction of text conversations: SMS, WhatsApp and the Chat API (#314).

A voice call's lines are redacted as they are stored, because the model never
reads them back: it has the call's own context. A text conversation's model
does — `sms_chat._build_llm_messages` rebuilds every turn from the stored
`sms_messages` rows — so redacting them as they are written would make the
agent forget the number it was given one message ago. So, while the project
redacts:

- **While the session is live** its rows hold what was said, raw, for the
  model. Everything that reads them on the way out is redacted here
  (`readable_messages`, `readable_sessions`): the Chat API's message and
  session reads, and a scenario derived from the session. The webhooks
  (`chat.created`, `session.*`) are redacted at the event exit point.
- **Once it has ended** (expired: by its TTL, by `DELETE`, or replaced by a
  new one) the agent will never read it again, so its rows are rewritten
  redacted, as a call's are: `sms_messages.content` and the session's
  `customer_number` / `turncall_number`. The expiry marks the session
  `PENDING` when its project redacts at that moment (`sms_session_repo`), and
  the session sweep (`seal_ended_sessions`, every 15 minutes) does the rewrite
  and marks each row `DONE`, which the reads then pass through as stored.

A session that ended before its project turned redaction on is not rewritten,
as a call's stored lines are not; its reads are redacted all the same while
the project redacts. The rewrite never stores `<REDACTED>` for a detector
that did not answer: it waits for the next sweep instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from itertools import batched
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.session_state import DONE, PENDING, REDACTION
from turncall.services import redaction
from turncall.storage.models import SmsSessionRow
from turncall.storage.repositories import sms_message_repo, sms_session_repo

# The Chat API's stand-in for a session with no number: TurnCall's, not text.
NO_NUMBER = "api"
# Messages per detector job when a session is rewritten: a long conversation
# must not take the worker's whole backlog (`redaction.MAX_PENDING`) at once.
REWRITE_BATCH = 64
# Sessions rewritten per sweep.
REWRITE_LIMIT = 50
# `sms_sessions.customer_number` / `turncall_number` are VARCHAR(20).
_NUMBER_LENGTH = 20


# --- On the way out, while a session may be live ---------------------------


async def readable_messages(
    db: AsyncSession, project_id: UUID, rows: Sequence[Any]
) -> list[Any]:
    """`rows` (`sms_messages`) as they may leave: their content redacted when
    the project redacts, except a row already rewritten. Rows that change come
    back as copies: an ORM row is never touched, which would write it back."""
    policy = await redaction.policy_for_project(project_id, session=db)
    raw = [row for row in rows if not _done(row)]
    if policy is None or not raw:
        return list(rows)
    await _release(db)
    contents = await redaction.redact_json([row.content for row in raw], policy)
    redacted = {row.id: content for row, content in zip(raw, contents, strict=True)}
    return [
        _copy(row, content=redacted[row.id]) if row.id in redacted else row
        for row in rows
    ]


async def readable_sessions(
    db: AsyncSession, project_id: UUID, rows: Sequence[Any]
) -> list[Any]:
    """`rows` (`sms_sessions`) as they may leave: their numbers redacted when
    the project redacts, except a session already rewritten."""
    policy = await redaction.policy_for_project(project_id, session=db)
    raw = [row for row in rows if not _done(row)]
    if policy is None or not raw:
        return list(rows)
    await _release(db)
    numbers = await asyncio.gather(*(_numbers(row, policy) for row in raw))
    redacted = {row.id: pair for row, pair in zip(raw, numbers, strict=True)}
    return [
        _copy(
            row,
            customer_number=redacted[row.id][0],
            turncall_number=redacted[row.id][1],
        )
        if row.id in redacted
        else row
        for row in rows
    ]


# --- Once a session has ended ------------------------------------------------


async def seal_ended_sessions(
    sessions: Callable[[], AsyncSession], *, limit: int = REWRITE_LIMIT
) -> int:
    """Rewrite redacted the rows of sessions that ended while their project
    redacted (marked `PENDING` by their expiry). Returns how many were
    rewritten. A detector that does not answer ends the pass: the rest wait
    for the next sweep, raw at rest and redacted on every read meanwhile."""
    async with sessions() as db:
        pending = await sms_session_repo.list_pending_redaction(db, limit=limit)
    sealed = 0
    for row in pending:
        try:
            if await _seal(sessions, row):
                sealed += 1
        except redaction.RedactionWithheld:
            logger.warning(
                "text_session_redaction_deferred",
                session_id=str(row.id),
                reason="the detector did not answer",
            )
            break
    return sealed


async def _seal(sessions: Callable[[], AsyncSession], row: SmsSessionRow) -> bool:
    """One ended session's rows, rewritten. The detector is waited on with no
    session open (events/emit's rule): read, close, redact, then write."""
    async with sessions() as db:
        policy = await redaction.policy_for_project(row.project_id, session=db)
        if policy is None:
            # Turned off between the expiry and now: nothing to rewrite.
            await sms_session_repo.clear_pending_redaction(db, row.id)
            await db.commit()
            return False
        messages = await sms_message_repo.list_unredacted_messages(db, row.id)

    contents: list[str] = []
    for chunk in batched((content for _, content in messages), REWRITE_BATCH):
        contents += await redaction.redact_json(list(chunk), policy, strict=True)
    customer, turncall = await _numbers(row, policy, strict=True)

    async with sessions() as db:
        for (message_id, _), content in zip(messages, contents, strict=True):
            await sms_message_repo.set_message_redacted(db, message_id, content)
        # A turn that was mid-reply as the session expired may have stored
        # its answer since: the session stays pending for the next sweep.
        late = await sms_message_repo.list_unredacted_messages(db, row.id)
        await sms_session_repo.set_session_redacted(
            db,
            row.id,
            customer_number=_fit(customer),
            turncall_number=_fit(turncall),
            state=PENDING if late else DONE,
        )
        await db.commit()
    logger.info("text_session_redacted", session_id=str(row.id), messages=len(messages))
    return not late


# --- Helpers -------------------------------------------------------------------


async def _numbers(
    row: Any, policy: redaction.RedactionPolicy, *, strict: bool = False
) -> tuple[str, str]:
    """A session's two numbers redacted: replaced whole when they are numbers
    (a WhatsApp id too), analysed when they are text (a chat's customer may be
    an email), the Chat API's placeholder left as it is."""
    fields = {
        key: value
        for key, value in (
            ("customer_number", row.customer_number),
            ("turncall_number", row.turncall_number),
        )
        if value != NO_NUMBER
    }
    out = await redaction.redact_payload(fields, policy, strict=strict)
    return (
        out.get("customer_number", row.customer_number),
        out.get("turncall_number", row.turncall_number),
    )


def _fit(number: str) -> str:
    """A redacted value that still fits its column: replacing a short email
    with `<EMAIL_ADDRESS>` can lengthen it."""
    return number if len(number) <= _NUMBER_LENGTH else redaction.WITHHELD


def _done(row: Any) -> bool:
    return (row.metadata_json or {}).get(REDACTION) == DONE


def _copy(row: Any, **changes: Any) -> SimpleNamespace:
    """A detached copy of an ORM row's columns, with `changes`."""
    columns = {
        attr.key: getattr(row, attr.key) for attr in inspect(row).mapper.column_attrs
    }
    return SimpleNamespace(**{**columns, **changes})


async def _release(db: AsyncSession) -> None:
    """End the read-only transaction before waiting on the detector, so the
    wait holds no connection (events/emit)."""
    if db.in_transaction():
        await db.commit()
