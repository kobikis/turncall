"""The `do_not_call` built-in against a real Postgres (#228).

Driven through the tool bridge's built-in dispatch, so this is the path a model
calling the tool takes: the caller's number lands on the project's suppression
list, `call.opted_out` is recorded, and the call is marked as ended by the
agent. The hangup itself is the pipeline's, once the goodbye has played
(`test_ending_tool_waits_for_goodbye.py`), so the row is still live here.
"""

import json
import uuid

import pytest
from sqlalchemy import delete, select

from turncall.config.settings import Settings
from turncall.domain.enums import CallEventType
from turncall.orchestrator.pipeline_factory import CallContext
from turncall.orchestrator.tool_bridge import _execute_builtin
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import CallEventRow, ProjectRow, SuppressedNumberRow
from turncall.storage.repositories import call_repo, suppression_repo

pytestmark = pytest.mark.integration

CALLER = "+15550000042"
OURS = "+15550000099"


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.execute(select(SuppressedNumberRow).limit(1))
    except Exception:
        pytest.skip("Postgres with suppressed_numbers not reachable")
    created: list[uuid.UUID] = []
    session_factory.created = created  # type: ignore[attr-defined]
    yield session_factory
    async with session_factory() as session:
        await session.execute(delete(ProjectRow).where(ProjectRow.id.in_(created)))
        await session.commit()
    await engine.dispose()


async def _live_call(factory, *, direction: str, from_number, to_number):
    async with factory() as session:
        project = ProjectRow(name=f"dnc-{uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        factory.created.append(project.id)
        call = await call_repo.create_call(
            session,
            project_id=project.id,
            direction=direction,
            from_number=from_number,
            to_number=to_number,
            provider_call_sid="webrtc",  # no carrier to hang up on
        )
        await call_repo.update_call_status(session, call.id, status="in_progress")
        await session.commit()
    return CallContext(
        call_id=call.id,
        project_id=project.id,
        agent_id=uuid.uuid4(),
        call_sid="webrtc",
        stream_sid="",
        session_factory=factory,
    )


async def _events(factory, call_id) -> list[CallEventRow]:
    async with factory() as session:
        result = await session.execute(
            select(CallEventRow).where(CallEventRow.call_id == call_id)
        )
        return list(result.scalars().all())


@pytest.mark.parametrize(
    ("direction", "from_number", "to_number"),
    [("inbound", CALLER, OURS), ("outbound", OURS, CALLER)],
)
async def test_suppresses_the_caller_and_records_the_end(
    factory, direction, from_number, to_number
):
    ctx = await _live_call(
        factory, direction=direction, from_number=from_number, to_number=to_number
    )

    result = json.loads(await _execute_builtin("do_not_call", {}, ctx))

    assert result["success"] is True
    async with factory() as session:
        assert await suppression_repo.is_suppressed(
            session, project_id=ctx.project_id, number=CALLER
        )
        assert not await suppression_repo.is_suppressed(
            session, project_id=ctx.project_id, number=OURS
        )
        call = await call_repo.get_call_by_id(session, ctx.call_id)
    assert call.status == "in_progress"
    events = await _events(factory, ctx.call_id)
    opted = [e for e in events if e.event_type == CallEventType.CALL_OPTED_OUT]
    assert [e.payload for e in opted] == [{"number": CALLER}]
    ended = [e for e in events if e.event_type == CallEventType.CALL_ENDED]
    assert [e.payload for e in ended] == [{"reason": "opted_out", "source": "control"}]


async def test_end_call_records_the_end_and_leaves_the_hangup_to_the_pipeline(
    factory,
):
    ctx = await _live_call(
        factory, direction="inbound", from_number=CALLER, to_number=OURS
    )

    result = json.loads(await _execute_builtin("end_call", {"reason": "done"}, ctx))

    assert result["success"] is True
    async with factory() as session:
        call = await call_repo.get_call_by_id(session, ctx.call_id)
    assert call.status == "in_progress"
    events = await _events(factory, ctx.call_id)
    assert [(e.event_type, e.payload) for e in events] == [
        (CallEventType.CALL_ENDED, {"reason": "done", "source": "control"})
    ]


async def test_a_call_with_no_phone_number_is_refused_and_left_running(factory):
    ctx = await _live_call(
        factory, direction="inbound", from_number=None, to_number=None
    )

    result = json.loads(await _execute_builtin("do_not_call", {}, ctx))

    assert result["success"] is False
    async with factory() as session:
        call = await call_repo.get_call_by_id(session, ctx.call_id)
    assert call.status == "in_progress"
    assert await _events(factory, ctx.call_id) == []
