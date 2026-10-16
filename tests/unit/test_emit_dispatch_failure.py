"""A failed send cannot cost the stored row (#312 review).

`emit_call_event` stores the event on the caller's session and, unless asked
to commit first, leaves the commit to the caller. The send reads the
subscriber list and the call's agent on that same session. In Postgres a
failed statement aborts the whole transaction, so a read that fails during
the send would make the caller's commit fail and lose the row, while
`emit_call_event` logs the send error and carries on as if nothing were
wrong. The exit point runs the send in a SAVEPOINT; this pins that.

`PgSession` models the part of Postgres that matters: a failed statement
aborts the transaction unless a savepoint is rolled back to. The real-database
check is `tests/integration/test_emit_dispatch_failure.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from turncall.events import dispatcher, emit
from turncall.storage.repositories import call_repo

pytestmark = pytest.mark.unit


class Aborted(Exception):
    """`current transaction is aborted, commands ignored until end of
    transaction block`."""


class PgSession:
    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.committed: list[dict[str, Any]] = []
        self.aborted = False

    def fail(self) -> None:
        """A statement on this session failed."""
        self.aborted = True
        raise DBAPIError("SELECT ...", {}, Exception("connection reset"))

    def begin_nested(self) -> Savepoint:
        return Savepoint(self)

    async def get(self, *_: Any) -> None:
        """The exit point reads the project's redaction (#313): no project."""
        return None

    async def commit(self) -> None:
        if self.aborted:
            raise Aborted
        self.committed += self.pending
        self.pending = []


class Savepoint:
    def __init__(self, session: PgSession) -> None:
        self.session = session

    async def __aenter__(self) -> Savepoint:
        self.aborted_before = self.session.aborted
        self.pending_before = list(self.session.pending)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:  # ROLLBACK TO SAVEPOINT
            self.session.aborted = self.aborted_before
            self.session.pending = self.pending_before
        return False


@pytest.fixture
def session(monkeypatch) -> PgSession:
    session = PgSession()

    async def next_sequence_number(_session, _call_id) -> int:
        return 1

    async def create_call_event(_session, **row: Any) -> None:
        session.pending.append(row)

    async def get_active_subscribers(_session, _project_id, _event_type):
        session.fail()

    monkeypatch.setattr(call_repo, "get_next_sequence_number", next_sequence_number)
    monkeypatch.setattr(call_repo, "create_call_event", create_call_event)
    monkeypatch.setattr(dispatcher, "get_active_subscribers", get_active_subscribers)
    return session


async def test_a_failed_send_leaves_the_stored_row_to_the_callers_commit(session):
    project_id = uuid4()
    await emit.emit_call_event(
        session,  # type: ignore[arg-type]
        call_id=uuid4(),
        project_id=project_id,
        event_type="transcript.final",
        payload=await emit.redact(
            project_id, "transcript.final", {"role": "customer", "text": "hello"}
        ),
    )
    await session.commit()

    assert [row["payload"] for row in session.committed] == [
        {"role": "customer", "text": "hello"}
    ]


async def test_a_send_only_entry_still_raises_but_leaves_the_session_usable(
    session,
):
    session.pending.append({"payload": "the caller's own work"})

    with pytest.raises(DBAPIError):
        await emit.dispatch_project_event(
            session,  # type: ignore[arg-type]
            project_id=uuid4(),
            event_type="chat.created",
            payload={},
        )
    await session.commit()

    assert session.committed == [{"payload": "the caller's own work"}]
