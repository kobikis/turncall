"""The two phone-number queries services/twilio_accounts relies on (#191),
against a real Postgres: which sealed token signs an account's webhooks, and
whether any number carries its own account at all."""

import uuid

import pytest
from sqlalchemy import delete, select

from turncall.config.settings import Settings
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import PhoneNumberRow, ProjectRow
from turncall.storage.repositories import phone_number_repo
from turncall.storage.repositories.phone_number_repo import StoredTwilioAccount

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


async def _bind(factory, account: StoredTwilioAccount | None) -> None:
    async with factory() as session:
        project = ProjectRow(name=f"twilio-q-{uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        factory.created.append(project.id)
        await phone_number_repo.bind_phone_number(
            session,
            project_id=project.id,
            external_number_sid=f"PN{uuid.uuid4().hex}",
            e164_number=f"+1555{uuid.uuid4().int % 10**7:07d}",
            routing_target_type="agent",
            twilio_account=account,
        )
        await session.commit()


async def test_the_sealed_token_is_found_by_its_account_sid(factory) -> None:
    sid = f"AC{uuid.uuid4().hex}"
    await _bind(factory, StoredTwilioAccount(sid, "sealed"))
    await _bind(factory, None)
    async with factory() as session:
        assert (
            await phone_number_repo.token_ciphertext_for_account(session, sid)
            == "sealed"
        )
        assert (
            await phone_number_repo.token_ciphertext_for_account(session, "ACnobody")
            is None
        )
        assert await phone_number_repo.any_has_own_twilio_account(session) is True


async def test_platform_twilio_numbers_are_counted(factory) -> None:
    """The startup warning's count (#337): Twilio numbers on the platform
    account, not those carrying their own."""
    async with factory() as session:
        before = await phone_number_repo.count_on_platform_twilio_account(session)
    await _bind(factory, None)
    await _bind(factory, None)
    await _bind(factory, StoredTwilioAccount(f"AC{uuid.uuid4().hex}", "sealed"))
    async with factory() as session:
        after = await phone_number_repo.count_on_platform_twilio_account(session)
    assert after - before == 2
