"""Process-wide shared aiohttp ClientSession.

Pipecat's ElevenLabs STT and the HeyGen/Tavus avatar services take an
``aiohttp.ClientSession`` and only *borrow* it — they never close it. Building
one per call therefore leaked a session (and its connector's open sockets) on
every ElevenLabs/avatar call. This hands out one shared session; the app
lifespan closes it on shutdown. Mirrors adapters/http_client.py.
"""

from __future__ import annotations

import aiohttp

_session: aiohttp.ClientSession | None = None


def get_aiohttp_session() -> aiohttp.ClientSession:
    """Return the shared session, creating it lazily (must be called inside a
    running event loop). Never close the returned session — its lifecycle is
    owned by the app lifespan (close_aiohttp_session)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_aiohttp_session() -> None:
    """Close the shared session (app shutdown)."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
