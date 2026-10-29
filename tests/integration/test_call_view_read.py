"""The call read against a real Postgres (#175).

Seeds calls, their events and agents, then reads them through the HTTP routes.
The loader batches per page, so this is also where an N+1 would show up as a
wrong answer for the second call rather than only as slowness.
"""

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from turncall.api.deps import get_session
from turncall.app import create_app
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.config.settings import Settings
from turncall.domain.enums import CallEventType, ProjectRole
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectRow
from turncall.storage.repositories import agent_repo, call_repo

pytestmark = pytest.mark.integration

ENDED = datetime(2026, 9, 30, 12, 42, tzinfo=UTC)


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.connection()
    except Exception:
        pytest.skip("Postgres not reachable")
    yield session_factory
    await engine.dispose()


async def _ended_call(session, project_id, *, agent_id=None, control=False):
    call = await call_repo.create_call(
        session, project_id=project_id, direction="inbound", active_agent_id=agent_id
    )
    await call_repo.update_call_status(
        session, call.id, status="completed", ended_at=ENDED
    )
    await call_repo.create_call_event(
        session,
        call_id=call.id,
        event_type=CallEventType.CALL_ENDED,
        payload={"source": "control"} if control else {},
        sequence_number=1,
    )
    return call


@pytest.fixture
async def seeded(factory):
    async with factory() as session:
        project = ProjectRow(name=f"call-view-{uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        agent = await agent_repo.create_agent(
            session, project_id=project.id, name="support", version=4, config_blob={}
        )
        stored = await _ended_call(session, project.id, agent_id=agent.id, control=True)
        inline = await _ended_call(session, project.id)
        for n, ms in ((1, 900), (2, 1500)):
            await call_repo.create_call_event(
                session,
                call_id=stored.id,
                event_type=CallEventType.METRICS_TURN,
                payload={"turn": n, "user_to_bot_ms": ms, "stages": {}},
                sequence_number=1 + n,
            )
        await session.commit()
    return project.id, stored.id, inline.id


@pytest.fixture
async def client(factory, seeded):
    project_id = seeded[0]
    app = create_app(Settings())

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[resolve_auth_context] = lambda: AuthContext(
        project_id=project_id, api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_single_read_carries_derived_fields(client, seeded):
    _, stored_id, inline_id = seeded
    stored = (await client.get(f"/v1/calls/{stored_id}")).json()["data"]
    assert stored["ended_reason"] == "assistant_ended_call"
    assert (stored["agent_name"], stored["agent_version"]) == ("support", 4)
    assert stored["latency_summary"]["slowest_turn"] == {
        "turn": 2,
        "user_to_bot_ms": 1500,
    }

    inline = (await client.get(f"/v1/calls/{inline_id}")).json()["data"]
    assert inline["ended_reason"] == "customer_ended_call"
    assert inline["agent_name"] is None and inline["agent_version"] is None
    assert inline["latency_summary"] is None


async def test_list_carries_each_calls_own_fields(client, seeded):
    _, stored_id, inline_id = seeded
    items = {c["id"]: c for c in (await client.get("/v1/calls")).json()["data"]}
    assert items[str(stored_id)]["ended_reason"] == "assistant_ended_call"
    assert items[str(stored_id)]["agent_name"] == "support"
    assert items[str(inline_id)]["ended_reason"] == "customer_ended_call"
    assert items[str(inline_id)]["agent_name"] is None
    assert items[str(stored_id)]["latency_summary"]["turns"] == 2
    assert items[str(inline_id)]["latency_summary"] is None


async def test_deleted_agent_row_reads_as_null(factory, client, seeded):
    _, stored_id, _ = seeded
    async with factory() as session:
        call = await call_repo.get_call_by_id(session, stored_id)
        agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)
        await session.delete(agent)
        await session.commit()
    body = (await client.get(f"/v1/calls/{stored_id}")).json()["data"]
    assert body["agent_name"] is None and body["agent_version"] is None


async def test_call_ended_payload_matches_the_read(factory, client, seeded):
    """The webhook takes its derived fields from the same function as the read."""
    from unittest.mock import AsyncMock, patch

    from turncall.domain.models import AnalysisConfig, LLMConfig
    from turncall.services import call_analysis_trigger

    project_id, stored_id, _ = seeded
    async with factory() as session:
        await call_repo.update_call_recording_status(session, stored_id, "failed")
        await session.commit()
    dispatch = AsyncMock()
    with patch("turncall.events.dispatcher.dispatch_event", dispatch):
        await call_analysis_trigger._run_post_call(
            factory,
            stored_id,
            project_id,
            AnalysisConfig(enabled=False),
            LLMConfig(),
            "",
        )
    payload = dispatch.await_args.kwargs["payload"]
    read = (await client.get(f"/v1/calls/{stored_id}")).json()["data"]
    for field in (
        "ended_reason",
        "agent_name",
        "agent_version",
        "latency_summary",
        "failovers",
        "skills",
    ):
        assert payload[field] == read[field], field
    assert read["skills"] == []  # no skills: an empty list, as `failovers`


async def test_the_skills_a_call_ran_read_the_same_everywhere(factory, client, seeded):
    """#425: the resolution saved on the row plus the call's `skill.loaded`
    events — every resolved skill, loaded or not, on the single read, the list
    and `call.ended`; the stored resolution never shows in `metadata`."""
    from unittest.mock import AsyncMock, patch

    from turncall.domain.models import AnalysisConfig, LLMConfig
    from turncall.services import call_analysis_trigger
    from turncall.services.call_view import RESOLVED_SKILLS

    project_id, _, call_id = seeded
    async with factory() as session:
        call = await call_repo.get_call_by_id(session, call_id)
        call.metadata_json = {"crm_id": "C-1"}
        await session.commit()
        await call_repo.merge_call_metadata(
            session,
            call_id,
            {
                # Stored as before #426, with no `pinned`: read as false.
                RESOLVED_SKILLS: [
                    {"id": str(uuid.uuid4()), "skill": "verify", "version": 2},
                    {"id": str(uuid.uuid4()), "skill": "explain_bill", "version": 1},
                ]
            },
        )
        await call_repo.create_call_event(
            session,
            call_id=call_id,
            event_type=CallEventType.SKILL_LOADED,
            payload={"skill": "verify", "version": 2, "reason": "load_skill"},
            sequence_number=9,
        )
        await session.commit()

    expected = [
        {"skill": "verify", "version": 2, "pinned": False, "loaded": True},
        {"skill": "explain_bill", "version": 1, "pinned": False, "loaded": False},
    ]
    read = (await client.get(f"/v1/calls/{call_id}")).json()["data"]
    assert read["skills"] == expected
    assert read["metadata"] == {"crm_id": "C-1"}  # merged beside, not over
    items = {c["id"]: c for c in (await client.get("/v1/calls")).json()["data"]}
    assert items[str(call_id)]["skills"] == expected

    dispatch = AsyncMock()
    with patch("turncall.events.dispatcher.dispatch_event", dispatch):
        await call_analysis_trigger._run_post_call(
            factory, call_id, project_id, AnalysisConfig(enabled=False), LLMConfig(), ""
        )
    payload = dispatch.await_args.kwargs["payload"]
    assert payload["skills"] == expected
    assert RESOLVED_SKILLS not in payload["metadata"]


async def test_an_inline_agents_config_never_leaves_the_call(factory, client, seeded):
    """#276: the inline config carries the call's credentials (MCP headers, an
    inline `llm.api_key`). The pipeline and post-call work read it; nothing
    outbound carries it — not the read, not `call.ended`."""
    from unittest.mock import AsyncMock, patch

    from turncall.domain.models import AnalysisConfig, LLMConfig
    from turncall.services import call_analysis_trigger

    project_id = seeded[0]
    inline_config = {
        "llm": {"provider": "openai", "api_key": "sk-secret"},
        "mcp_servers": [{"name": "pd", "headers": {"Authorization": "Bearer t"}}],
    }
    async with factory() as session:
        call = await call_repo.create_call(
            session,
            project_id=project_id,
            direction="inbound",
            metadata_json={"crm_id": "C-123", "dynamic_config": inline_config},
        )
        await call_repo.update_call_status(
            session, call.id, status="completed", ended_at=ENDED
        )
        await session.commit()

    one = (await client.get(f"/v1/calls/{call.id}")).json()["data"]
    listed = {c["id"]: c for c in (await client.get("/v1/calls")).json()["data"]}
    dispatch = AsyncMock()
    with patch("turncall.events.dispatcher.dispatch_event", dispatch):
        await call_analysis_trigger._run_post_call(
            factory, call.id, project_id, AnalysisConfig(enabled=False), LLMConfig(), ""
        )
    ended = dispatch.await_args.kwargs["payload"]

    for metadata in (
        one["metadata"],
        listed[str(call.id)]["metadata"],
        ended["metadata"],
    ):
        assert metadata == {"crm_id": "C-123"}
    async with factory() as session:  # still there for whoever runs the call
        row = await call_repo.get_call_by_id(session, call.id)
        assert (
            await call_analysis_trigger.config_for_call(session, row) == inline_config
        )
