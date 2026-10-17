"""Whether any number takes WhatsApp (#339), against a real Postgres: what the
startup warning about a missing WHATSAPP_APP_SECRET counts as configured."""

import uuid

import pytest
from sqlalchemy import delete, select

from turncall.config.settings import Settings
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import PhoneNumberRow, ProjectRow
from turncall.storage.repositories import phone_number_repo

pytestmark = pytest.mark.integration


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.execute(select(PhoneNumberRow.id).limit(1))
    except Exception:
        pytest.skip("Postgres with phone_numbers not reachable")
    created: list[uuid.UUID] = []
    session_factory.created = created  # type: ignore[attr-defined]
    yield session_factory
    async with session_factory() as session:
        await session.execute(delete(ProjectRow).where(ProjectRow.id.in_(created)))
        await session.commit()
    await engine.dispose()


async def _bind(factory, *, whatsapp: bool) -> None:
    async with factory() as session:
        project = ProjectRow(name=f"whatsapp-q-{uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        factory.created.append(project.id)
        row = await phone_number_repo.bind_phone_number(
            session,
            project_id=project.id,
            external_number_sid=f"PN{uuid.uuid4().hex}",
            e164_number=f"+1555{uuid.uuid4().int % 10**7:07d}",
            routing_target_type="agent",
        )
        row.whatsapp_enabled = whatsapp
        await session.commit()


async def _any(factory) -> bool:
    async with factory() as session:
        return await phone_number_repo.any_whatsapp_enabled(session)


async def test_only_a_whatsapp_enabled_number_counts(factory) -> None:
    before = await _any(factory)

    await _bind(factory, whatsapp=False)
    assert await _any(factory) is before

    await _bind(factory, whatsapp=True)
    assert await _any(factory) is True
