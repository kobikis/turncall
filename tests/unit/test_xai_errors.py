"""One rule for a key xAI refuses, on any xAI service (#392).

xAI answers a wrong key with HTTP 400 on every endpoint (measured: the STT and
TTS handshakes, `POST /v1/tts`, `/chat/completions`), which pipecat files as a
bad request. `call_session` reads only AUTHENTICATION/AUTHORIZATION as a
rejected key, so the rule turns that 400 into AUTHENTICATION wherever it is
mixed in.
"""

import httpx
import openai
import pytest
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.utils.errors import ErrorCategory

from turncall.orchestrator.xai_errors import XAIKeyRefusal

REFUSED = {
    "code": "invalid-argument",
    "error": "Incorrect API key provided. You can obtain an API key from https://console.x.ai.",
}


class _Service(XAIKeyRefusal, FrameProcessor):
    """Stands in for any pipecat xAI service: the rule is a mixin over
    pipecat's own `_classify_error` hook."""


def _handshake(status: int, body: bytes) -> Exception:
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    return InvalidStatus(Response(status, "", Headers(), body))


def _sdk(status: int, body: dict) -> Exception:
    """What the OpenAI SDK raises for a 400 from an OpenAI-compatible
    endpoint: the JSON body on `.body`, not in its message."""
    request = httpx.Request("POST", "https://api.x.ai/v1/chat/completions")
    response = httpx.Response(status, request=request, json=body)
    return openai.BadRequestError("Error code: 400", response=response, body=body)


def _category(exception: Exception) -> ErrorCategory | None:
    return _Service()._classify_error(exception)


class TestARefusedKeyIsAuthentication:
    def test_on_a_websocket_handshake(self) -> None:
        refused = _handshake(400, str(REFUSED).encode())
        assert _category(refused) is ErrorCategory.AUTHENTICATION

    def test_on_the_openai_sdk(self) -> None:
        assert _category(_sdk(400, REFUSED)) is ErrorCategory.AUTHENTICATION


class TestAnyOtherBadRequestIsLeftAlone:
    """An unknown model is the agent's mistake, not the key's: left to
    pipecat, which files a 400 as INVALID_REQUEST."""

    @pytest.mark.parametrize(
        "error",
        [
            _handshake(400, b'{"error":"Model grok-voice-9 not found"}'),
            _sdk(400, {"error": "Model grok-9 not found"}),
        ],
        ids=["websocket", "openai_sdk"],
    )
    def test_is_not_classified_here(self, error: Exception) -> None:
        assert _category(error) is None

    def test_a_key_named_in_another_status_is_not_reread(self) -> None:
        """A 401 or 403 is already classified by pipecat, and a 5xx naming
        a key is still the server's failure."""
        assert _category(_handshake(500, b"API key service unavailable")) is None
