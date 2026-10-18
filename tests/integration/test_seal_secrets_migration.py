"""The `seal_secrets_at_rest` migration against a real Postgres (#338).

Each test gets a database of its own, migrated to the revision before this
one and seeded with plaintext secrets in raw SQL, then runs alembic itself in
a subprocess: the real env.py, so the one-transaction-per-run is the real one
too. A refusal, or a failure halfway through, must leave every row as it was:
a half-sealed database is the thing the transaction is there to prevent.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
BEFORE = "d4b8e2f6a0c3"
REVISION = "e6c2a8f4b0d9"
PREFIX = "tcs1:"

CONFIG: dict[str, Any] = {
    "name": "support",
    "system_prompt": "Be kind.",
    "llm": {
        "provider": "openai",
        "api_key": "sk-agent-key",
        "failover": [{"provider": "anthropic", "api_key": "sk-failover-key"}],
    },
    "aws": {
        "region": "us-east-1",
        "access_key_id": "AKIAAGENTKEY",
        "secret_access_key": "aws-secret-key",
        "session_token": "aws-session-token",
    },
    "server_url": {"url": "https://example.com/events", "secret": "server-secret"},
    "tools": [{"name": "lookup", "webhook_secret": "tool-webhook-secret"}],
    "mcp_servers": [
        {
            "name": "crm",
            "headers": {"Authorization": "Bearer mcp-header-token"},
            "env": {"CRM_TOKEN": "mcp-env-token"},
        }
    ],
}
CONFIG_SECRETS = {
    "sk-agent-key",
    "sk-failover-key",
    "AKIAAGENTKEY",
    "aws-secret-key",
    "aws-session-token",
    "server-secret",
    "tool-webhook-secret",
    "Bearer mcp-header-token",
    "mcp-env-token",
}
WEBHOOK_SECRET = "whsec-0123456789abcdefghijklmnopqrstuvwxyzABC"
NUMBER_SECRET = "srv-0123456789abcdefghijklmnopqrstuvwxyzABCD"


def _base_url() -> Any:
    return make_url(
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://turncall:turncall@localhost:5432/turncall",
        )
    )


async def _connect(url: Any, database: str | None = None) -> asyncpg.Connection:
    return await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=database or url.database,
    )


@pytest.fixture
def database() -> Iterator[str]:
    """A fresh database at the revision before this one; its URL."""
    url = _base_url()
    name = f"{url.database}_seal_{uuid.uuid4().hex[:8]}"

    async def admin(sql: str) -> None:
        server = await _connect(url, "postgres")
        try:
            await server.execute(sql)
        finally:
            await server.close()

    try:
        asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Postgres not reachable")
    own = url.set(database=name).render_as_string(hide_password=False)
    try:
        migrated = _alembic(own, "upgrade", BEFORE, key=None)
        assert migrated.returncode == 0, migrated.stderr
        yield own
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _alembic(url: str, *args: str, key: str | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url}
    env.pop("CREDENTIAL_ENCRYPTION_KEY", None)
    if key is not None:
        env["CREDENTIAL_ENCRYPTION_KEY"] = key
    # A process of its own: alembic's env.py reconfigures logging.
    return subprocess.run(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _run(url: str, fn: Any) -> Any:
    async def go() -> Any:
        conn = await _connect(make_url(url))
        try:
            return await fn(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


async def _seed(conn: asyncpg.Connection) -> None:
    project = uuid.uuid4()
    config = json.dumps(CONFIG)
    await conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES ($1, 'seal', now(), now())",
        project,
    )
    await conn.execute(
        "INSERT INTO webhook_subscriptions "
        "(id, project_id, url, secret, events, active, created_at) "
        "VALUES ($1, $2, 'https://example.com/hook', $3, '[]', true, now())",
        uuid.uuid4(),
        project,
        WEBHOOK_SECRET,
    )
    for e164, secret in (("+15550100", NUMBER_SECRET), ("+15550101", None)):
        await conn.execute(
            "INSERT INTO phone_numbers (id, project_id, provider, "
            "external_number_sid, e164_number, routing_target_type, "
            "server_url_secret, metadata_json, created_at) "
            "VALUES ($1, $2, 'twilio', $3, $3, 'agent', $4, '{}', now())",
            uuid.uuid4(),
            project,
            e164,
            secret,
        )
    for name, blob in (("support", config), ("plain", '{"name": "plain"}')):
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, environment, version, "
            "state, config_blob, created_at) "
            "VALUES ($1, $2, $3, 'production', 1, 'published', $4::jsonb, now())",
            uuid.uuid4(),
            project,
            name,
            blob,
        )
    for metadata in (
        {"dynamic_config": CONFIG, "caller": "+15550199"},
        {"caller": "+15550198"},
    ):
        await conn.execute(
            "INSERT INTO calls (id, project_id, provider, direction, status, "
            "recording_status, metadata_json, created_at) "
            "VALUES ($1, $2, 'twilio', 'inbound', 'completed', 'none', $3::jsonb, "
            "now())",
            uuid.uuid4(),
            project,
            json.dumps(metadata),
        )
    await conn.execute(
        "INSERT INTO sms_sessions (id, project_id, customer_number, "
        "turncall_number, expires_at, metadata_json) "
        "VALUES ($1, $2, '+15550199', '+15550100', now(), $3::jsonb)",
        uuid.uuid4(),
        project,
        json.dumps({"dynamic_config": CONFIG, "redaction": "pending"}),
    )
    target = json.dumps({"type": "inline", "agent": CONFIG})
    scenario = uuid.uuid4()
    await conn.execute(
        "INSERT INTO eval_scenarios (id, project_id, name, kind, definition, "
        "schema_version, default_target, created_at, updated_at) "
        "VALUES ($1, $2, 'greets', 'script', '{}', '1', $3::jsonb, now(), now())",
        scenario,
        project,
        target,
    )
    await conn.execute(
        "INSERT INTO eval_runs (id, project_id, scenario_id, scenario_name, kind, "
        "target, resolved_config, resolved_scenario, harness_config, modality, "
        "queued_at) VALUES ($1, $2, $3, 'greets', 'script', $4::jsonb, '{}', '{}', "
        "'{}', 'text', now())",
        uuid.uuid4(),
        project,
        scenario,
        target,
    )


# (table, column, holds JSON)
SNAPSHOT = (
    ("webhook_subscriptions", "secret", False),
    ("phone_numbers", "server_url_secret", False),
    ("agents", "config_blob", True),
    ("calls", "metadata_json", True),
    ("sms_sessions", "metadata_json", True),
    ("eval_scenarios", "default_target", True),
    ("eval_runs", "target", True),
)


async def _snapshot(conn: asyncpg.Connection) -> dict[str, Any]:
    """Every column the migration touches, by row, plus the schema state."""
    out: dict[str, Any] = {}
    for table, column, is_json in SNAPSHOT:
        # The names are this file's constants, never input.
        query = f"SELECT id, {column} AS v FROM {table} ORDER BY id"  # noqa: S608
        rows = await conn.fetch(query)
        out[f"{table}.{column}"] = [
            (r["id"], json.loads(r["v"]) if is_json else r["v"]) for r in rows
        ]
    out["version"] = await conn.fetchval("SELECT version_num FROM alembic_version")
    out["types"] = [
        tuple(r)
        for r in await conn.fetch(
            "SELECT table_name, data_type, character_maximum_length "
            "FROM information_schema.columns WHERE (table_name, column_name) IN "
            "(('webhook_subscriptions', 'secret'), "
            "('phone_numbers', 'server_url_secret')) ORDER BY table_name"
        )
    ]
    return out


def _unseal(key: str, value: Any) -> Any:
    """`value` with every sealed string in it decrypted; asserts each one is."""
    fernet = Fernet(key.encode())
    if isinstance(value, dict):
        return {k: _unseal(key, v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_unseal(key, v) for v in value)
    if isinstance(value, str) and value.startswith(PREFIX):
        return fernet.decrypt(value[len(PREFIX) :].encode()).decode()
    return value


def _seeded(url: str) -> dict[str, Any]:
    async def go(conn: asyncpg.Connection) -> dict[str, Any]:
        await _seed(conn)
        return await _snapshot(conn)

    return _run(url, go)


def test_seals_every_secret_and_unseals_it_on_downgrade(database):
    key = Fernet.generate_key().decode()
    before = _seeded(database)

    up = _alembic(database, "upgrade", REVISION, key=key)
    assert up.returncode == 0, up.stderr
    after = _run(database, _snapshot)

    assert after["version"] == REVISION
    assert {data_type for _, data_type, _ in after["types"]} == {"text"}
    dumped = json.dumps(
        {k: v for k, v in after.items() if k not in ("version", "types")},
        default=str,
    )
    for secret in {*CONFIG_SECRETS, WEBHOOK_SECRET, NUMBER_SECRET}:
        assert secret not in dumped, secret

    [(_, webhook)] = after["webhook_subscriptions.secret"]
    assert webhook.startswith(PREFIX)
    sealed_numbers = [v for _, v in after["phone_numbers.server_url_secret"] if v]
    assert len(sealed_numbers) == 1
    assert sealed_numbers[0].startswith(PREFIX)
    agent = next(v for _, v in after["agents.config_blob"] if v["name"] == "support")
    for value in (
        agent["llm"]["api_key"],
        agent["llm"]["failover"][0]["api_key"],
        agent["aws"]["access_key_id"],
        agent["aws"]["secret_access_key"],
        agent["aws"]["session_token"],
        agent["server_url"]["secret"],
        agent["tools"][0]["webhook_secret"],
        agent["mcp_servers"][0]["headers"]["Authorization"],
        agent["mcp_servers"][0]["env"]["CRM_TOKEN"],
    ):
        assert value.startswith(PREFIX)
    assert agent["system_prompt"] == "Be kind."
    assert agent["aws"]["region"] == "us-east-1"

    # Every value decrypts to what was there; everything else is untouched.
    for name in before:
        if name in ("version", "types"):
            continue
        assert _unseal(key, after[name]) == before[name], name

    down = _alembic(database, "downgrade", BEFORE, key=key)
    assert down.returncode == 0, down.stderr
    restored = _run(database, _snapshot)
    assert restored == before

    again = _alembic(database, "upgrade", REVISION, key=key)
    assert again.returncode == 0, again.stderr
    resealed = _run(database, _snapshot)
    for name in before:
        if name in ("version", "types"):
            continue
        assert _unseal(key, resealed[name]) == before[name], name


def test_refuses_without_a_key_and_changes_nothing(database):
    before = _seeded(database)

    up = _alembic(database, "upgrade", REVISION, key=None)

    assert up.returncode != 0
    assert "hold secrets in plaintext" in up.stderr
    assert "CREDENTIAL_ENCRYPTION_KEY is not set" in up.stderr
    assert _run(database, _snapshot) == before


def test_a_failure_halfway_through_rolls_every_row_back(database):
    key = Fernet.generate_key().decode()
    before = _seeded(database)

    # The last table sealed refuses its update, after every other table's
    # rows have been rewritten in the same transaction.
    async def trap(conn: asyncpg.Connection) -> None:
        await conn.execute(
            "CREATE FUNCTION seal_trap() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'seal trap'; END $$"
        )
        await conn.execute(
            "CREATE TRIGGER seal_trap BEFORE UPDATE ON eval_scenarios "
            "FOR EACH ROW EXECUTE FUNCTION seal_trap()"
        )

    _run(database, trap)
    up = _alembic(database, "upgrade", REVISION, key=key)

    assert up.returncode != 0
    assert "seal trap" in up.stderr
    assert _run(database, _snapshot) == before


def test_downgrade_refuses_without_the_key_and_changes_nothing(database):
    key = Fernet.generate_key().decode()
    _seeded(database)
    assert _alembic(database, "upgrade", REVISION, key=key).returncode == 0
    sealed = _run(database, _snapshot)

    down = _alembic(database, "downgrade", BEFORE, key=None)
    assert down.returncode != 0
    assert "hold sealed secrets" in down.stderr
    assert _run(database, _snapshot) == sealed

    # Under a key they were not sealed with, the unseal itself refuses.
    other = _alembic(database, "downgrade", BEFORE, key=Fernet.generate_key().decode())
    assert other.returncode != 0
    assert "rotated" in other.stderr
    assert _run(database, _snapshot) == sealed


def test_a_database_without_secrets_upgrades_without_a_key(database):
    up = _alembic(database, "upgrade", REVISION, key=None)
    assert up.returncode == 0, up.stderr
    down = _alembic(database, "downgrade", BEFORE, key=None)
    assert down.returncode == 0, down.stderr
