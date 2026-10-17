"""The text-conversation sweep and reads, against a real Postgres (#314).

Every API replica runs the session sweep, so a session is claimed before it
is rewritten and two sweeps never rewrite one; the claim is an index lookup
however many sessions have ever expired; a reply stored as its session is
rewritten is never left raw under a session marked done; and a read of a
long conversation goes to the detector in jobs of `DETECT_BATCH`, so it
cannot fill the backlog live calls share. The detector is a stand-in here:
the real one, end to end, is `test_harness_text_redaction.py`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, text, update

from turncall.config.settings import Settings
from turncall.domain.session_state import CLAIMED_UNTIL, DONE, PENDING, REDACTION
from turncall.services import redaction, text_redaction
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectRow, SmsMessageRow, SmsSessionRow
from turncall.storage.repositories import sms_message_repo, sms_session_repo

pytestmark = pytest.mark.integration

NUMBER = "415 555 0132"
SAID = f"call me on {NUMBER}"
REDACTED = "call me on <PHONE_NUMBER>"
# Older than anything another test leaves pending: claimed first.
LONG_AGO = datetime(2001, 1, 1, tzinfo=UTC)


class _Detector:
    """Stands in for the worker: replaces NUMBER, records each job, and how
    many texts were in flight at once."""

    def __init__(self, delay: float = 0.0) -> None:
        self.jobs: list[list[str]] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self._delay = delay

    async def redact(self, texts: list[str], _entities: Any) -> list[str]:
        self.jobs.append(list(texts))
        self.in_flight += len(texts)
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
            return [t.replace(NUMBER, "<PHONE_NUMBER>") for t in texts]
        finally:
            self.in_flight -= len(texts)


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.connection()
    except Exception:
        pytest.skip("Postgres not reachable")
    redaction.forget_policies()
    yield session_factory
    redaction.forget_policies()
    await engine.dispose()


@pytest.fixture
def detector(monkeypatch) -> _Detector:
    stand_in = _Detector(delay=0.05)
    monkeypatch.setattr(redaction.ENGINE, "redact", stand_in.redact)
    return stand_in


@pytest.fixture
async def project(factory):
    """A redacting project, and afterwards its sessions gone: the database is
    shared, and a session left pending would take a later sweep's slot."""
    async with factory() as db:
        row = ProjectRow(name=f"sweep-{uuid.uuid4().hex[:6]}", redaction_enabled=True)
        db.add(row)
        await db.commit()
        project_id = row.id
    yield project_id
    async with factory() as db:
        await db.execute(
            delete(SmsSessionRow).where(SmsSessionRow.project_id == project_id)
        )
        await db.execute(delete(ProjectRow).where(ProjectRow.id == project_id))
        await db.commit()


async def _session(
    factory: Any,
    project_id: uuid.UUID,
    *,
    messages: int = 2,
    ended: bool = True,
    customer: str = "+14155550132",
    label: str = "",
) -> uuid.UUID:
    """A session with `messages` messages saying SAID; `ended`: expired while
    its project redacts (so pending), long ago."""
    async with factory() as db:
        row = await sms_session_repo.create_session(
            db,
            project_id=project_id,
            agent_id=None,
            customer_number=customer,
            turncall_number="+14155550199",
            channel="chat",
        )
        for i in range(messages):
            await sms_message_repo.create_message(
                db,
                session_id=row.id,
                project_id=project_id,
                role="customer" if i % 2 == 0 else "assistant",
                content=f"{SAID} ({label}{i})",
                channel="chat",
            )
        await db.execute(
            update(SmsSessionRow)
            .where(SmsSessionRow.id == row.id)
            .values(expires_at=LONG_AGO)
        )
        if ended:
            assert await sms_session_repo.expire_session(db, row.id) is not None
        await db.commit()
        return row.id


async def _state(factory: Any, session_id: uuid.UUID) -> tuple[dict, list[str]]:
    async with factory() as db:
        row = await db.get(SmsSessionRow, session_id)
        assert row is not None
        contents = (
            await db.scalars(
                select(SmsMessageRow.content)
                .where(SmsMessageRow.session_id == session_id)
                .order_by(SmsMessageRow.created_at)
            )
        ).all()
        return dict(row.metadata_json), list(contents)


async def test_two_sweeps_at_once_rewrite_each_session_once(
    factory, detector, project
) -> None:
    """Two replicas' sweeps at the same moment: each session is claimed by
    one of them, so the detector reads its messages once."""
    labels = [uuid.uuid4().hex[:8] for _ in range(3)]
    ids = [await _session(factory, project, label=label) for label in labels]

    sealed = await asyncio.gather(
        text_redaction.seal_ended_sessions(factory),
        text_redaction.seal_ended_sessions(factory),
    )

    assert sum(sealed) >= len(ids)
    for session_id, label in zip(ids, labels, strict=True):
        metadata, contents = await _state(factory, session_id)
        assert metadata[REDACTION] == DONE
        assert CLAIMED_UNTIL not in metadata
        assert contents == [f"{REDACTED} ({label}0)", f"{REDACTED} ({label}1)"]
        analysed = [t for job in detector.jobs for t in job if f"({label}" in t]
        assert sorted(analysed) == [f"{SAID} ({label}0)", f"{SAID} ({label}1)"]


async def test_a_claimed_session_waits_for_its_lease(factory, project) -> None:
    """A sweep that died mid-rewrite keeps its sessions until the lease runs
    out, then any sweep takes them."""
    session_id = await _session(factory, project)
    lease = timedelta(minutes=10)

    async with factory() as db:
        first = await sms_session_repo.claim_pending_redaction(
            db, limit=50, lease=lease
        )
        await db.commit()
    async with factory() as db:
        again = await sms_session_repo.claim_pending_redaction(
            db, limit=50, lease=lease
        )
        await db.commit()
        # The lease ran out (its replica died).
        await db.execute(
            update(SmsSessionRow)
            .where(SmsSessionRow.id == session_id)
            .values(
                metadata_json=SmsSessionRow.metadata_json.op("||")(
                    text(
                        f"jsonb_build_object('{CLAIMED_UNTIL}', now() - interval '1 s')"
                    )
                )
            )
        )
        await db.commit()
    async with factory() as db:
        after = await sms_session_repo.claim_pending_redaction(
            db, limit=50, lease=lease
        )
        await sms_session_repo.release_redaction_claims(db, [r.id for r in after])
        await db.commit()

    assert session_id in {r.id for r in first}
    assert session_id not in {r.id for r in again}
    assert session_id in {r.id for r in after}
    metadata, _ = await _state(factory, session_id)
    assert metadata[REDACTION] == PENDING
    assert CLAIMED_UNTIL not in metadata


async def test_a_reply_stored_as_its_session_is_rewritten_is_not_left_raw(
    factory, detector, project, monkeypatch
) -> None:
    """A turn that was mid-reply as its session expired stores its answer
    just as the rewrite checks for late messages, and commits before the
    rewrite does. The session must not end up `done` with that answer raw:
    the answer waits for the rewrite's commit, then puts the session back to
    pending, and the next sweep rewrites it."""
    session_id = await _session(factory, project)
    late_reply = f"{SAID} (late)"
    reply_stored = asyncio.Event()
    writers: list[asyncio.Task[None]] = []
    list_unredacted = sms_message_repo.list_unredacted_messages
    calls = 0

    async def reply() -> None:
        async with factory() as db:
            await sms_message_repo.create_message(
                db,
                session_id=session_id,
                project_id=project,
                role="assistant",
                content=late_reply,
                channel="chat",
            )
            await db.commit()
        reply_stored.set()

    async def checked_then_replied(db: Any, sid: uuid.UUID) -> Any:
        nonlocal calls
        found = await list_unredacted(db, sid)
        if sid == session_id:
            calls += 1
            if calls == 2:  # the rewrite's check for late messages
                writers.append(asyncio.create_task(reply()))
                try:
                    await asyncio.wait_for(reply_stored.wait(), timeout=0.5)
                except TimeoutError:
                    pass  # waiting on the rewrite's lock, as it should
        return found

    monkeypatch.setattr(
        sms_message_repo, "list_unredacted_messages", checked_then_replied
    )
    await text_redaction.seal_ended_sessions(factory)
    await asyncio.gather(*writers)
    metadata, contents = await _state(factory, session_id)

    assert contents[-1] == late_reply
    assert metadata[REDACTION] == PENDING

    await text_redaction.seal_ended_sessions(factory)
    metadata, contents = await _state(factory, session_id)
    assert metadata[REDACTION] == DONE
    assert contents == [f"{REDACTED} (0)", f"{REDACTED} (1)", f"{REDACTED} (late)"]


async def test_the_claim_is_an_index_lookup(factory, project) -> None:
    """Every replica claims every 15 minutes: with thousands of sessions
    expired and done, it reads the partial index of the pending ones."""
    async with factory() as db:
        await db.execute(
            SmsSessionRow.__table__.insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "project_id": project,
                    "customer_number": "+14155550132",
                    "turncall_number": "+14155550199",
                    "status": "expired",
                    "channel": "chat",
                    "message_count": 0,
                    "last_activity_at": LONG_AGO,
                    "expires_at": LONG_AGO,
                    "metadata_json": {REDACTION: DONE},
                }
                for _ in range(3000)
            ],
        )
        await db.commit()
    await _session(factory, project)

    async with factory() as db:
        await db.execute(text("ANALYZE sms_sessions"))
        statement = (
            select(SmsSessionRow.id)
            .where(
                SmsSessionRow.status == "expired",
                sms_session_repo._REDACTION_PENDING,
                sms_session_repo._UNCLAIMED,
            )
            .order_by(SmsSessionRow.expires_at.asc())
            .limit(50)
            .with_for_update(skip_locked=True)
        )
        connection = await db.connection()
        compiled = statement.compile(
            dialect=connection.dialect, compile_kwargs={"literal_binds": True}
        )
        plan = (
            await connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {compiled}")
        ).scalar_one()
    plan = json.loads(plan) if isinstance(plan, str) else plan

    assert "ix_sms_sessions_redaction_pending" in json.dumps(plan)
    assert '"Seq Scan"' not in json.dumps(plan)


async def test_a_long_conversation_is_read_in_jobs(factory, detector, project) -> None:
    """A page of a thousand messages, or of hundreds of sessions, goes to the
    detector DETECT_BATCH texts at a time, never all at once: the backlog
    (`MAX_PENDING`) is shared with live calls, whose text would be withheld
    while one read filled it."""
    session_id = await _session(factory, project, messages=300, ended=False)
    emails = [
        await _session(
            factory, project, messages=0, ended=False, customer=f"j{i}@example.com"
        )
        for i in range(80)
    ]

    async with factory() as db:
        rows = await sms_message_repo.list_messages_for_session(
            db, session_id, limit=1000
        )
        read = await text_redaction.readable_messages(db, project, rows)
    async with factory() as db:
        sessions = [await db.get(SmsSessionRow, sid) for sid in emails]
        await text_redaction.readable_sessions(db, project, sessions)

    assert [m.content for m in read] == [f"{REDACTED} ({i})" for i in range(300)]
    assert max(len(job) for job in detector.jobs) <= text_redaction.DETECT_BATCH
    assert detector.most_in_flight <= text_redaction.DETECT_BATCH
