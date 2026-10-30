"""Project credentials against a real Postgres (ADR-0020, #162).

The upsert is one `ON CONFLICT` statement, which only Postgres can exercise.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, select, text

from turncall.config.settings import Settings
from turncall.domain.config_secrets import map_secrets
from turncall.orchestrator.pipeline_builder import load_call_credentials
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectCredentialRow, ProjectRow
from turncall.storage.repositories import credential_repo
from turncall.storage.sealing import seal

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


async def test_startup_check_opens_the_secrets_sealed_at_rest(factory, settings):
    """The LIKE samples run against Postgres, and fail closed on what they find.

    Written with raw SQL, as stored: whether the columns seal on their own is
    the column types' business, not this check's.
    """
    pid = await _project(factory)
    config = map_secrets(
        {"system_prompt": "hi", "llm": {"provider": "openai", "api_key": "sk-x"}},
        lambda v: seal(v, settings),
    )
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO webhook_subscriptions"
                " (id, project_id, url, secret, events, active, created_at)"
                " VALUES (:id, :pid, 'https://x', :secret, '{}', true, now())"
            ),
            {"id": uuid.uuid4(), "pid": pid, "secret": seal("s", settings)},
        )
        await session.execute(
            text(
                "INSERT INTO agents (id, project_id, name, environment, version,"
                " state, config_blob, created_at) VALUES (:id, :pid, 'sealed',"
                " 'development', 1, 'draft', CAST(:config AS jsonb), now())"
            ),
            {"id": uuid.uuid4(), "pid": pid, "config": json.dumps(config)},
        )
        await session.commit()
    unset = SimpleNamespace(credentials=SimpleNamespace(encryption_key=""))
    other = SimpleNamespace(
        credentials=SimpleNamespace(encryption_key=Fernet.generate_key().decode())
    )
    async with factory() as session:
        with pytest.raises(credential_repo.CredentialKeyError):
            await credential_repo.ensure_readable(session, unset)
        with pytest.raises(credential_repo.CredentialKeyError, match="rotated"):
            await credential_repo.ensure_readable(session, other)
        await credential_repo.ensure_readable(session, settings)


async def _only_what_the_test_adds(session) -> None:
    """Blank every stored secret, in this transaction only: the startup check
    then sees just the rows the test writes, not what other tests left. The
    caller rolls it back."""
    for sql in (
        "DELETE FROM project_credentials",
        "UPDATE phone_numbers SET twilio_auth_token_ciphertext = NULL,"
        " server_url_secret = NULL",
        "UPDATE webhook_subscriptions SET secret = 'plain'",
        "UPDATE agents SET config_blob = '{}'",
        "UPDATE calls SET metadata_json = '{}'",
        "UPDATE sms_sessions SET metadata_json = '{}'",
        "UPDATE eval_runs SET target = '{}'",
        "UPDATE eval_scenarios SET default_target = NULL",
    ):
        await session.execute(text(sql))


def _other_key() -> SimpleNamespace:
    return SimpleNamespace(
        credentials=SimpleNamespace(encryption_key=Fernet.generate_key().decode())
    )


@pytest.mark.parametrize("table", ["calls", "sms_sessions", "eval_scenarios"])
async def test_startup_check_opens_an_inline_config_alone(factory, table):
    """A database whose only sealed secrets are inline configs — a call's or a
    session's call-init answer, an eval target — still fails closed on them."""
    pid = await _project(factory)
    sealed_under = _other_key()
    config = map_secrets(
        {"llm": {"provider": "openai", "api_key": "sk-x"}},
        lambda v: seal(v, sealed_under),
    )
    insert = {
        "calls": "INSERT INTO calls (id, project_id, provider, direction, status,"
        " recording_status, metadata_json, created_at) VALUES (:id, :pid,"
        " 'twilio', 'inbound', 'completed', 'none', CAST(:doc AS jsonb), now())",
        "sms_sessions": "INSERT INTO sms_sessions (id, project_id,"
        " customer_number, turncall_number, expires_at, metadata_json) VALUES"
        " (:id, :pid, '+15550199', '+15550100', now(), CAST(:doc AS jsonb))",
        "eval_scenarios": "INSERT INTO eval_scenarios (id, project_id, name, kind,"
        " definition, schema_version, default_target, created_at, updated_at)"
        " VALUES (:id, :pid, 'greets', 'script', '{}', '1', CAST(:doc AS jsonb),"
        " now(), now())",
    }[table]
    document = (
        {"type": "inline", "agent": config}
        if table == "eval_scenarios"
        else {"dynamic_config": config, "caller": "+15550199"}
    )
    async with factory() as session:
        await _only_what_the_test_adds(session)
        await session.execute(
            text(insert), {"id": uuid.uuid4(), "pid": pid, "doc": json.dumps(document)}
        )
        with pytest.raises(credential_repo.CredentialKeyError, match="rotated"):
            await credential_repo.ensure_readable(session, _other_key())
        await credential_repo.ensure_readable(session, sealed_under)
        await session.rollback()


async def test_startup_check_looks_past_a_prompt_that_mentions_the_prefix(factory):
    """The first agent matching `"tcs1:` has it only in its prompt; the check
    walks on to one whose secret really is sealed."""
    pid = await _project(factory)
    sealed_under = _other_key()
    prompt = {"system_prompt": 'reply with "tcs1:" first', "llm": {"model": "x"}}
    sealed = map_secrets(
        {"llm": {"provider": "openai", "api_key": "sk-x"}},
        lambda v: seal(v, sealed_under),
    )
    insert = text(
        "INSERT INTO agents (id, project_id, name, environment, version, state,"
        " config_blob, created_at) VALUES (:id, :pid, :name, 'development', 1,"
        " 'draft', CAST(:config AS jsonb), now())"
    )
    unset = SimpleNamespace(credentials=SimpleNamespace(encryption_key=""))
    async with factory() as session:
        await _only_what_the_test_adds(session)
        await session.execute(
            insert,
            {"id": uuid.uuid4(), "pid": pid, "name": "p", "config": json.dumps(prompt)},
        )
        await credential_repo.ensure_readable(session, unset)  # no secret: no key
        await session.execute(
            insert,
            {"id": uuid.uuid4(), "pid": pid, "name": "s", "config": json.dumps(sealed)},
        )
        with pytest.raises(credential_repo.CredentialKeyError):
            await credential_repo.ensure_readable(session, unset)
        await credential_repo.ensure_readable(session, sealed_under)
        await session.rollback()


async def test_a_secret_written_without_the_key_raises_the_key_error(
    factory, monkeypatch
):
    """The column type seals in SQLAlchemy's bind step, which wraps what it
    raises; the engine's listener raises CredentialKeyError as itself, so
    every `except CredentialKeyError` sees it (#338)."""
    from sqlalchemy.exc import StatementError

    from turncall.config.settings import get_settings
    from turncall.storage.models import WebhookSubscriptionRow

    pid = await _project(factory)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    try:
        async with factory() as session:
            session.add(
                WebhookSubscriptionRow(
                    project_id=pid, url="https://x", secret="whsec", events=[]
                )
            )
            with pytest.raises(credential_repo.CredentialKeyError) as raised:
                await session.flush()
            assert not isinstance(raised.value, StatementError)
            await session.rollback()
    finally:
        get_settings.cache_clear()


async def test_startup_check_passes_an_unmigrated_database(factory):
    """A first run starts TurnCall before `make migrate`: no tables, so no
    secrets, and nothing to fail on. It used to stop startup on the first
    query (`relation "project_credentials" does not exist`)."""
    async with factory() as session:
        schema = f"empty_{uuid.uuid4().hex}"
        await session.execute(text(f"CREATE SCHEMA {schema}"))
        await session.execute(text(f"SET LOCAL search_path TO {schema}"))
        await credential_repo.ensure_readable(session, _other_key())
        await session.rollback()
