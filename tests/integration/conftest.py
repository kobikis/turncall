"""Fixtures shared by the integration tests."""

import pytest

from tests.integration.limits_support import REDIS_URL


@pytest.fixture
async def redis_env(monkeypatch):
    """The app's Redis, on a database index of its own; skip without one."""
    from redis.asyncio import Redis

    client = Redis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis not reachable")
    finally:
        await client.aclose()
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
