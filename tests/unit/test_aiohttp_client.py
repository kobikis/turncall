"""Shared aiohttp session: one instance reused, recreated after close."""

import pytest

from turncall.adapters import aiohttp_client


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_and_reclosable():
    a = aiohttp_client.get_aiohttp_session()
    b = aiohttp_client.get_aiohttp_session()
    assert a is b  # borrowed, not per-call
    await aiohttp_client.close_aiohttp_session()
    assert a.closed
    c = aiohttp_client.get_aiohttp_session()
    assert c is not a and not c.closed  # recreated lazily after close
    await aiohttp_client.close_aiohttp_session()
