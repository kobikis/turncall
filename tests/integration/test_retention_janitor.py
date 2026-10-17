"""The retention janitor against a real Postgres, on an injected clock (#316).

Each sweep is given its `now`: a call that ended at `ENDED` is swept at
`ENDED + n days`, so a test walks a call through its retention without
waiting for it. What the API reads after a sweep, end to end, is
`test_harness_retention.py`.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select

from turncall.adapters.storage import LocalStorageAdapter
from turncall.config.settings import Settings
from turncall.domain.enums import CallEventType
from turncall.services import retention
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import (
    AgentRow,
    CallEventRow,
    CallRow,
    ProjectRow,
    ToolInvocationRow,
)

pytestmark = pytest.mark.integration

ENDED = datetime(2026, 1, 1, 12, tzinfo=UTC)
CALL_DATA = {"calls", "call_events", "tool_invocations"}
# The events a call stores, by the kind they go with.
SAID = (CallEventType.TRANSCRIPT_FINAL, CallEventType.TOOL_RESULT)
OTHER = (CallEventType.CALL_STARTED, CallEventType.METRICS_TURN)


def _days(n: float) -> datetime:
    return ENDED + timedelta(days=n)


@pytest.fixture
async def db():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.connection()
    except Exception:
        pytest.skip("Postgres not reachable")
    yield engine, session_factory
    await engine.dispose()


@pytest.fixture
def factory(db):
    return db[1]


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageAdapter:
    return LocalStorageAdapter(base_path=str(tmp_path))


@pytest.fixture
async def projects(factory):
    """Projects made by `_project`, and afterwards gone with their calls: the
    database is shared, and a project left with a retention set would be
    swept by every later test."""
    made: list[uuid.UUID] = []
    yield made
    async with factory() as db:
        await db.execute(delete(ProjectRow).where(ProjectRow.id.in_(made)))
        await db.commit()


async def _project(factory: Any, projects: list[uuid.UUID], **days: int) -> uuid.UUID:
    async with factory() as db:
        row = ProjectRow(
            name=f"retention-{uuid.uuid4().hex[:6]}",
            **{f"retention_{kind}": value for kind, value in days.items()},
        )
        db.add(row)
        await db.commit()
        projects.append(row.id)
        return row.id


async def _agent(
    factory: Any,
    project_id: uuid.UUID,
    *,
    version: int,
    state: str,
    retention: dict[str, int] | None = None,
    name: str = "support",
) -> uuid.UUID:
    async with factory() as db:
        row = AgentRow(
            project_id=project_id,
            name=name,
            version=version,
            state=state,
            config_blob={"retention": retention} if retention else {},
        )
        db.add(row)
        await db.commit()
        return row.id


async def _call(
    factory: Any,
    storage: LocalStorageAdapter,
    project_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    ended_at: datetime | None = ENDED,
    created_at: datetime | None = None,
    status: str = "completed",
) -> uuid.UUID:
    """A call with all three kinds of data: a recording, a transcript (lines,
    a tool call and its invocation row, an analysis) and other events,
    including the agent's hangup marker its `ended_reason` comes from."""
    call_id = uuid.uuid4()
    url = await storage.upload(f"recordings/{call_id}.wav", b"RIFF")
    async with factory() as db:
        db.add(
            CallRow(
                id=call_id,
                project_id=project_id,
                direction="inbound",
                from_number="+15550001234",
                to_number="+15550009999",
                active_agent_id=agent_id,
                status=status,
                started_at=(created_at or ENDED) + timedelta(seconds=1),
                ended_at=ended_at,
                created_at=created_at or ENDED - timedelta(minutes=5),
                duration_ms=60_000,
                recording_status="completed",
                recording_url=url,
                metadata_json={"crm_id": "42"},
                analysis_json={"summary": "The caller asked for a refund."},
            )
        )
        await db.flush()
        types = [*SAID, *OTHER, CallEventType.CALL_ENDED]
        for seq, event_type in enumerate(types):
            payload = {"source": "control"} if event_type == "call.ended" else {}
            db.add(
                CallEventRow(
                    call_id=call_id,
                    event_type=event_type,
                    payload=payload,
                    sequence_number=seq,
                )
            )
        db.add(
            ToolInvocationRow(
                call_id=call_id,
                tool_name="lookup",
                input_json={"order": "A1"},
                status="succeeded",
            )
        )
        await db.commit()
    return call_id


async def _state(factory: Any, call_id: uuid.UUID) -> dict[str, Any]:
    async with factory() as db:
        call = await db.get(CallRow, call_id)
        assert call is not None, "the call row itself is never deleted"
        types = (
            await db.execute(
                select(CallEventRow.event_type)
                .where(CallEventRow.call_id == call_id)
                .order_by(CallEventRow.sequence_number)
            )
        ).scalars()
        invocations = await db.scalar(
            select(func.count())
            .select_from(ToolInvocationRow)
            .where(ToolInvocationRow.call_id == call_id)
        )
        return {
            "call": call,
            "events": list(types),
            "invocations": invocations,
        }


def _recorded(storage: LocalStorageAdapter, call_id: uuid.UUID) -> bool:
    return (Path(storage._base_path) / f"recordings/{call_id}.wav").exists()


async def test_unset_everywhere_keeps_everything(factory, storage, projects) -> None:
    project_id = await _project(factory, projects)
    agent_id = await _agent(factory, project_id, version=1, state="published")
    call_id = await _call(factory, storage, project_id, agent_id=agent_id)
    before = await _state(factory, call_id)

    await retention.sweep(factory, storage, now=_days(36500))

    after = await _state(factory, call_id)
    assert after["events"] == before["events"] and len(after["events"]) == 5
    assert after["invocations"] == 1
    assert after["call"].analysis_json == {"summary": "The caller asked for a refund."}
    assert after["call"].recording_url is not None and _recorded(storage, call_id)
    assert after["call"].recording_expired_at is None
    assert after["call"].transcript_expired_at is None
    assert after["call"].events_expired_at is None


async def test_each_kind_goes_at_its_own_cutoff_and_the_row_stays(
    factory, storage, projects
) -> None:
    project_id = await _project(
        factory,
        projects,
        recordings_days=30,
        transcripts_days=60,
        events_days=90,
    )
    call_id = await _call(factory, storage, project_id)

    # A day early: nothing.
    await retention.sweep(factory, storage, now=_days(29))
    state = await _state(factory, call_id)
    assert len(state["events"]) == 5 and _recorded(storage, call_id)

    # The recording: the object and its URL.
    swept = await retention.sweep(factory, storage, now=_days(31))
    state = await _state(factory, call_id)
    assert swept.recording >= 1
    assert not _recorded(storage, call_id)
    assert state["call"].recording_url is None
    assert state["call"].recording_expired_at == _days(31)
    assert len(state["events"]) == 5 and state["invocations"] == 1

    # The transcript: its lines, the tool calls, the analysis.
    await retention.sweep(factory, storage, now=_days(61))
    state = await _state(factory, call_id)
    assert state["events"] == [*OTHER, CallEventType.CALL_ENDED]
    assert state["invocations"] == 0
    assert state["call"].analysis_json is None
    assert state["call"].transcript_expired_at == _days(61)

    # The rest of the event log; how the call ended is frozen on the row.
    await retention.sweep(factory, storage, now=_days(91))
    state = await _state(factory, call_id)
    call = state["call"]
    assert state["events"] == []
    assert call.events_expired_at == _days(91)
    assert call.ended_reason == "assistant_ended_call"
    # What stays: the call row.
    assert (call.from_number, call.to_number, call.status) == (
        "+15550001234",
        "+15550009999",
        "completed",
    )
    assert call.duration_ms == 60_000 and call.ended_at == ENDED
    assert call.metadata_json == {"crm_id": "42"}
    assert call.recording_status == "completed"


async def test_an_agent_overrides_its_project_for_every_version(
    factory, storage, projects
) -> None:
    """v2 is live and keeps transcripts 2 days, so a call v1 took is swept at
    2 days too; a call with no agent keeps the project's 30."""
    project_id = await _project(factory, projects, transcripts_days=30)
    v1 = await _agent(factory, project_id, version=1, state="archived")
    await _agent(
        factory,
        project_id,
        version=2,
        state="published",
        retention={"transcripts_days": 2},
    )
    on_v1 = await _call(factory, storage, project_id, agent_id=v1)
    no_agent = await _call(factory, storage, project_id)

    await retention.sweep(factory, storage, now=_days(3))

    assert (await _state(factory, on_v1))["call"].transcript_expired_at == _days(3)
    assert (await _state(factory, no_agent))["call"].transcript_expired_at is None
    await retention.sweep(factory, storage, now=_days(31))
    assert (await _state(factory, no_agent))["call"].transcript_expired_at == _days(31)


async def test_an_agent_alone_can_set_a_retention(factory, storage, projects) -> None:
    project_id = await _project(factory, projects)
    agent_id = await _agent(
        factory, project_id, version=1, state="draft", retention={"events_days": 1}
    )
    with_agent = await _call(factory, storage, project_id, agent_id=agent_id)
    no_agent = await _call(factory, storage, project_id)

    await retention.sweep(factory, storage, now=_days(2))

    assert (await _state(factory, with_agent))["events"] == list(SAID)
    assert len((await _state(factory, no_agent))["events"]) == 5


async def test_a_call_that_never_ended_counts_from_its_start(
    factory, storage, projects
) -> None:
    project_id = await _project(factory, projects, events_days=5)
    stuck = await _call(
        factory,
        storage,
        project_id,
        ended_at=None,
        created_at=_days(-10),
        status="in_progress",
    )

    await retention.sweep(factory, storage, now=_days(0))

    assert (await _state(factory, stuck))["events"] == list(SAID)


async def test_a_recording_that_could_not_be_deleted_is_kept_marked_unexpired(
    factory, storage, projects
) -> None:
    """Retried next sweep: a call marked expired with its object still there
    would be data kept past its retention that nothing names any more."""
    project_id = await _project(factory, projects, recordings_days=1)
    call_id = await _call(factory, storage, project_id)

    class Down(LocalStorageAdapter):
        async def delete(self, key: str) -> None:
            raise OSError("storage unreachable")

    await retention.sweep(factory, Down(str(storage._base_path)), now=_days(2))
    state = await _state(factory, call_id)
    assert state["call"].recording_expired_at is None
    assert state["call"].recording_url is not None

    await retention.sweep(factory, storage, now=_days(3))
    assert (await _state(factory, call_id))["call"].recording_expired_at == _days(3)
    assert not _recorded(storage, call_id)


async def test_two_janitors_never_sweep_one_call_twice(
    factory, storage, projects
) -> None:
    """Every API replica runs one: a batch is locked, the others skip it."""
    project_id = await _project(factory, projects, events_days=1)
    calls = [
        await _call(factory, storage, project_id) for _ in range(retention.BATCH + 20)
    ]

    one, two = await asyncio.gather(
        retention.sweep(factory, storage, now=_days(2)),
        retention.sweep(factory, storage, now=_days(2)),
    )

    assert one.events + two.events == len(calls)
    for call_id in calls[:: retention.BATCH // 4]:
        assert (await _state(factory, call_id))["events"] == list(SAID)


_WRITE = re.compile(
    r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.IGNORECASE
)


async def test_the_janitor_writes_nothing_but_call_data(db, storage, projects) -> None:
    """ADR-0024: call retention never touches the audit log (#317), which keeps
    its own. Held here for every table: a sweep that expires all three kinds
    writes to the call's own tables and nothing else."""
    engine, factory = db
    project_id = await _project(
        factory, projects, recordings_days=1, transcripts_days=1, events_days=1
    )
    call_id = await _call(factory, storage, project_id)
    written: set[str] = set()

    def record(_conn, _cursor, statement, *_args) -> None:
        if match := _WRITE.match(statement):
            written.add(match.group(1).lower())

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        swept = await retention.sweep(factory, storage, now=_days(2))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert swept.recording and swept.transcript and swept.events
    assert (await _state(factory, call_id))["events"] == []
    assert written and written <= CALL_DATA, written
