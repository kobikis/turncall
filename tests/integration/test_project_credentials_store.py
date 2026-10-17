"""Project credentials against a real Postgres (ADR-0020, #162).

The upsert is one `ON CONFLICT` statement, which only Postgres can exercise.
"""

import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, select

from turncall.config.settings import Settings
from turncall.orchestrator.pipeline_builder import load_call_credentials
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectCredentialRow, ProjectRow
from turncall.storage.repositories import credential_repo

pytestmark = pytest.mark.integration


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.execute(select(ProjectCredentialRow).limit(1))
    except Exception:
        pytest.skip("Postgres with project_credentials not reachable")
    created: list[uuid.UUID] = []
    session_factory.created = created  # type: ignore[attr-defined]
    yield session_factory
    # Rows encrypted under a throwaway key must not outlive the test: the
    # startup check would fail on them against the real one.
    async with session_factory() as session:
        await session.execute(delete(ProjectRow).where(ProjectRow.id.in_(created)))
        await session.commit()
    await engine.dispose()


@pytest.fixture
def settings():
    real = Settings()
    real.credentials.encryption_key = Fernet.generate_key().decode()
    return real


async def _project(factory) -> uuid.UUID:
    async with factory() as session:
        project = ProjectRow(name=f"creds-{uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.commit()
        factory.created.append(project.id)
        return project.id


async def test_written_is_read_back_and_replaced_in_place(factory, settings):
    pid = await _project(factory)
    async with factory() as session:
        await credential_repo.upsert(
            session,
            project_id=pid,
            provider="deepgram",
            document={"api_key": "first"},
            settings=settings,
        )
        await credential_repo.upsert(
            session,
            project_id=pid,
            provider="deepgram",
            document={"api_key": "second"},
            settings=settings,
        )
        await session.commit()

    async with factory() as session:
        rows = await credential_repo.list_for_project(session, pid)
        assert [r.provider for r in rows] == ["deepgram"]
        assert "second" not in rows[0].ciphertext

    creds = await load_call_credentials(factory, pid, settings)
    assert creds.api_key("deepgram") == "second"

    async with factory() as session:
        assert await credential_repo.delete_credential(session, pid, "deepgram")
        assert not await credential_repo.delete_credential(session, pid, "deepgram")
        await session.commit()
    creds = await load_call_credentials(factory, pid, settings)
    assert creds.api_key("deepgram") == settings.deepgram.api_key


async def test_a_replace_returns_what_it_wrote_to_a_session_that_read_it(
    factory, settings
):
    """The audit log reads the credential before a replace, in the same
    session (#317): the upsert must hand back the row as written, not the
    copy that read left in the session, or the replace records no change."""
    from turncall.services import audit_log

    pid = await _project(factory)
    async with factory() as session:
        await credential_repo.upsert(
            session,
            project_id=pid,
            provider="openai",
            document={"api_key": "first"},
            settings=settings,
        )
        await session.commit()
    async with factory() as session:
        held = await credential_repo.get(session, pid, "openai")  # kept alive
        assert held is not None
        before = audit_log.credential_state(held)
        old = held.ciphertext
        row = await credential_repo.upsert(
            session,
            project_id=pid,
            provider="openai",
            document={"api_key": "second"},
            settings=settings,
        )
        assert row.ciphertext != old
        assert audit_log.diff(before, audit_log.credential_state(row)) == {
            "/value": {"before": "***", "after": "***"}
        }
        await session.commit()


async def test_another_project_never_sees_it(factory, settings):
    mine, theirs = await _project(factory), await _project(factory)
    async with factory() as session:
        await credential_repo.upsert(
            session,
            project_id=mine,
            provider="openai",
            document={"api_key": "mine"},
            settings=settings,
        )
        await session.commit()
    async with factory() as session:
        assert await credential_repo.providers_for_project(session, theirs) == set()
        assert await credential_repo.providers_for_project(session, mine) == {"openai"}


async def test_startup_check_fails_closed(factory, settings):
    pid = await _project(factory)
    async with factory() as session:
        await credential_repo.upsert(
            session,
            project_id=pid,
            provider="tavus",
            document={"api_key": "k"},
            settings=settings,
        )
        await session.commit()
    unset = SimpleNamespace(credentials=SimpleNamespace(encryption_key=""))
    async with factory() as session:
        with pytest.raises(credential_repo.CredentialKeyError):
            await credential_repo.ensure_readable(session, unset)
        await credential_repo.delete_credential(session, pid, "tavus")
        await session.commit()
