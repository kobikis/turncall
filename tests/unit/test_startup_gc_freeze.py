"""The heap alive at startup is frozen before the app serves (#270).

A full collection scans every tracked object, and pipecat's import graph is
most of them: on a pod it paused every call's audio for 300-800 ms.
"""

import gc
from unittest.mock import AsyncMock

import pytest

from turncall import app as app_module


@pytest.mark.asyncio
async def test_startup_heap_is_frozen_before_serving(monkeypatch, settings) -> None:
    for name in ("init_database", "init_redis", "close_database", "close_redis"):
        monkeypatch.setattr(app_module, name, AsyncMock())
    monkeypatch.setattr(app_module, "_ensure_credentials_readable", AsyncMock())
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    gc.unfreeze()
    try:
        async with app_module.lifespan(app_module.create_app(settings)):
            # The interpreter parks a few hundred objects there on its own;
            # the startup heap is hundreds of thousands.
            assert gc.get_freeze_count() > 10_000
    finally:
        gc.unfreeze()
