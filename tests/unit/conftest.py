"""Fixtures shared by more than one unit test module."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def heygen_session(monkeypatch):
    """LiveAvatar grants a session; its room is the test's to play."""
    from pipecat.services.heygen.api_liveavatar import LiveAvatarApi
    from pipecat.services.heygen.client import HeyGenClient

    granted = SimpleNamespace(
        session_id="s1", ws_url="wss://x", livekit_url="wss://y", access_token="t"
    )
    monkeypatch.setattr(LiveAvatarApi, "new_session", AsyncMock(return_value=granted))
    monkeypatch.setattr(LiveAvatarApi, "close_session", AsyncMock())
    # The connections themselves: nothing for a unit test to reach.
    monkeypatch.setattr(HeyGenClient, "_ws_connect", AsyncMock())
    monkeypatch.setattr(HeyGenClient, "_livekit_connect", AsyncMock())
