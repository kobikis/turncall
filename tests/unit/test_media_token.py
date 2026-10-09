"""The media token (the media-stream takeover fix).

The behaviour — a stream without it never reaches the pipeline, on every
carrier — is `tests/integration/test_media_stream_takeover.py`. This pins the
token itself: bound to one call, keyed by the server secret, compared in
constant time.
"""

import hmac
import inspect
import uuid

import pytest

from turncall.config.settings import get_settings
from turncall.services import media_token as module
from turncall.services.media_token import media_token, media_token_valid

pytestmark = pytest.mark.unit


def test_a_token_opens_its_own_call_only():
    one, other = uuid.uuid4(), uuid.uuid4()
    assert media_token_valid(one, media_token(one))
    assert not media_token_valid(other, media_token(one))


@pytest.mark.parametrize("token", ["", "0" * 64, "not-hex", "é" * 64])
def test_anything_else_is_refused(token: str):
    assert not media_token_valid(uuid.uuid4(), token)


def test_the_key_is_the_server_secret(monkeypatch):
    call_id = uuid.uuid4()
    before = media_token(call_id)
    monkeypatch.setenv("API_KEY_HASH_SECRET", "another-deployment")
    get_settings.cache_clear()
    try:
        assert media_token(call_id) != before
    finally:
        get_settings.cache_clear()


def test_the_comparison_is_constant_time():
    source = inspect.getsource(module.media_token_valid)
    assert "compare_digest" in source
    assert "==" not in source
    assert module.hmac is hmac
