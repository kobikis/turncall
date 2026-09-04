"""Shared httpx client (review finding #9): one pooled instance per process."""

import pytest

from turncall.adapters import http_client


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_same_instance() -> None:
    await http_client.close_http_client()  # clean slate
    a = http_client.get_http_client()
    b = http_client.get_http_client()
    assert a is b  # pooled, not per-call
    await http_client.close_http_client()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recreates_after_close() -> None:
    a = http_client.get_http_client()
    await http_client.close_http_client()
    assert a.is_closed
    b = http_client.get_http_client()
    assert b is not a
    assert not b.is_closed
    await http_client.close_http_client()
