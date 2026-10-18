"""Shared test fixtures."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from turncall.app import create_app
from turncall.config.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Test settings (no real DB/Redis connections)."""
    return Settings()


@pytest.fixture
def app(settings: Settings) -> TestClient:
    """FastAPI test client (no lifespan — skips DB/Redis init)."""
    test_app = create_app(settings)

    # Override lifespan to avoid real DB/Redis connections in unit tests
    test_app.router.lifespan_context = None  # type: ignore[assignment]

    return TestClient(test_app, raise_server_exceptions=False)


def mcp_tool(name: str, schema: dict | None = None):
    """Build an mcp Tool.

    `input_schema` is 2.x's spelling, and 2.x is the only line reachable now
    that pipecat's mcp extra requires it (see test_dependency_pins). 1.x spelled
    it `inputSchema`, which survives on 2.x only as a pydantic alias.
    """
    from mcp.types import Tool

    return Tool(name=name, description="d", input_schema=schema or {"type": "object"})


def mcp_settings(*, allowed_url_patterns: list[str] | None = None, **mcp_over):
    """A Settings stand-in for the MCP tests.

    Built from MCPSettings' own field definitions rather than hand-listed, so a
    field added there can't leave these stubs behind — which is exactly what
    happened when `connect_timeout_seconds` arrived and four separately
    hand-rolled SimpleNamespaces all raised AttributeError.

    Declared defaults only, never the environment: these tests assert on
    specific limits, and a developer's .env must not change the answer.
    """
    from types import SimpleNamespace

    from turncall.config.settings import MCPSettings

    declared = {
        name: field.get_default(call_default_factory=True)
        for name, field in MCPSettings.model_fields.items()
    }
    return SimpleNamespace(
        mcp=SimpleNamespace(**{**declared, **mcp_over}),
        byom=SimpleNamespace(allowed_url_patterns=allowed_url_patterns or []),
    )


def creds(openai: str = "", **platform: str):
    """A `Credentials` for building services directly in a test (ADR-0020).

    The platform level is read from the environment as the app would — so a
    test's `monkeypatch.setenv("CARTESIA_API_KEY", ...)` still reaches the
    service — with explicit keyword overrides on top: `creds("sk-test")` for
    an OpenAI key, `creds(anthropic="sk-ant")` for another account.
    """
    from turncall.config.settings import Settings
    from turncall.services.credentials import Credentials, platform_keys

    keys = {**platform_keys(Settings()), **platform}
    if openai:
        keys["openai"] = openai
    return Credentials(platform=keys)


FROM_NUMBER = "+15550001234"
TO_NUMBER = "+15550009999"


def telemetry_call_session(monkeypatch):
    """A `CallSession` with just enough of a call for `_build_telemetry`.

    A recording live call whose row is an inbound Twilio call from
    `FROM_NUMBER` to `TO_NUMBER`; the repository read is faked, so no database.
    """
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    from turncall.orchestrator.call_session import CallSession
    from turncall.storage.repositories import call_repo

    @asynccontextmanager
    async def sessions():
        yield None

    async def the_call(_session, _call_id):
        return SimpleNamespace(
            direction="inbound",
            provider="twilio",
            from_number=FROM_NUMBER,
            to_number=TO_NUMBER,
        )

    monkeypatch.setattr(call_repo, "get_call_by_id", the_call)
    session = CallSession.__new__(CallSession)
    session._call_context = SimpleNamespace(
        project_id=uuid4(),
        agent_id=uuid4(),
        call_id=uuid4(),
        call_sid="CA1",
        records=True,
        is_eval=False,
        session_factory=sessions,
    )
    return session


@pytest.fixture(autouse=True)
def _events_carry_text_only_through_redact(request, monkeypatch):
    """A call site that hands an exit-point entry a plain dict with text in it,
    instead of redacting it before its transaction, fails its test whatever
    the project's redaction (events/emit, #313).

    Unit tests have no database: there, a project's redaction policy read on
    a session of its own (`emit.redact`) is off. A test that wants it on
    patches `redaction._read_policy_alone` itself."""
    from turncall.events import emit
    from turncall.services import redaction

    emit.STRICT = True
    redaction.forget_policies()
    if request.node.get_closest_marker("unit") is not None:

        async def off(_project_id, _sessions=None):
            return None

        monkeypatch.setattr(redaction, "_read_policy_alone", off)
    yield
    redaction.forget_policies()


@pytest.fixture(autouse=True)
def _logs_redacted_as_in_the_app():
    """loguru's patcher is installed as `setup_logging` installs it (the
    harness skips that), and no test starts inside a project's redaction
    another one left behind (services/telemetry_redaction, #314)."""
    from turncall.services import telemetry_redaction

    telemetry_redaction.install()
    telemetry_redaction.use(None)
    yield
    telemetry_redaction.use(None)


def pytest_sessionfinish(session, exitstatus) -> None:
    """Stop the redaction worker, which the harness keeps across apps
    (`run_app`), once every test is done."""
    redaction = sys.modules.get("turncall.services.redaction")
    if redaction is not None:
        redaction.shutdown()


def pytest_configure(config) -> None:
    """Under pytest-xdist, give each worker a database and a Redis database of
    its own, so tests running side by side never meet: a campaign worker or a
    sweep in one sees every project in its database, not only its test's."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        return
    from turncall.config.settings import get_settings

    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://turncall:turncall@localhost:5432/turncall",
    )
    if (own := _worker_database(base, worker)) is not None:
        os.environ["DATABASE_URL"] = own
    redis = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    # gw0 -> 1, gw1 -> 2, ...: database 0 stays the developer's own.
    os.environ["REDIS_URL"] = f"{redis.rsplit('/', 1)[0]}/{int(worker[2:]) + 1}"
    get_settings.cache_clear()


def _worker_database(base: str, worker: str) -> str | None:
    """A fresh, migrated `<db>_<worker>` beside the configured database, and
    its URL. None when Postgres isn't there, and the tests that need it skip
    as they would without xdist."""
    import asyncpg
    from sqlalchemy.engine import make_url

    url = make_url(base)
    name = f"{url.database}_{worker}"

    async def create() -> None:
        server = await asyncpg.connect(
            user=url.username,
            password=url.password,
            host=url.host,
            port=url.port or 5432,
            database="postgres",
        )
        try:
            await server.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await server.execute(f'CREATE DATABASE "{name}"')
        finally:
            await server.close()

    try:
        asyncio.run(create())
    except (OSError, asyncpg.PostgresError):
        return None
    own = url.set(database=name).render_as_string(hide_password=False)
    # A process of its own: alembic's env.py reconfigures logging.
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DATABASE_URL": own},
        capture_output=True,
        text=True,
    )
    if migrated.returncode != 0:
        raise RuntimeError(f"migrating {name} failed:\n{migrated.stderr}")
    return own
