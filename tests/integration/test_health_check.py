"""/health against a real Postgres.

It used to execute the isolation level's name ('READ COMMITTED') as a
statement, which SQLAlchemy refuses, so every deployment reported its database
unhealthy.
"""

import pytest
from sqlalchemy import text

from turncall.api.v1 import health
from turncall.config.settings import Settings
from turncall.storage.database import create_engine

pytestmark = pytest.mark.integration


async def test_reachable_database_is_healthy(monkeypatch):
    engine = create_engine(Settings().database)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not reachable")
    monkeypatch.setattr(health, "get_engine", lambda: engine)
    try:
        result = await health.health_check()
    finally:
        await engine.dispose()
    assert result["checks"]["database"] == "healthy"
