"""Process-wide pooled httpx client.

Constructing ``httpx.AsyncClient()`` per request pays a fresh TCP+TLS handshake
to the same hosts every time — several on latency-critical paths (call-init,
mid-call tool webhooks, LLM text completion). This module hands out one pooled,
keep-alive client shared across the process; the app lifespan closes it on
shutdown. Per-request ``timeout=`` overrides still apply.
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None

# Generous default; every call site still passes its own per-request timeout.
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


def get_http_client() -> httpx.AsyncClient:
    """Return the shared client, creating it lazily. Never close the returned
    client — its lifecycle is owned by the app lifespan (close_http_client)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, limits=_LIMITS)
    return _client


async def close_http_client() -> None:
    """Close the shared client (app shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
