"""A key xAI refuses, read as one on every xAI service (#386, #392).

Imports pipecat at module level, like the services that mix it in.
"""

from pipecat.utils.errors import ErrorCategory, extract_http_status_code


class XAIKeyRefusal:
    """Mixin, ahead of a pipecat xAI service in the bases, that reads xAI's
    refusal of a key as one.

    xAI answers a wrong key with HTTP 400 and "Incorrect API key provided" on
    every endpoint (measured), which pipecat files as a bad request, so the
    call's `error.raised` would not say which key to fix. A 401 or 403 is
    already classified by pipecat.
    """

    def _classify_error(self, exception: Exception) -> ErrorCategory | None:
        if extract_http_status_code(exception) == 400 and _names_a_key(exception):
            return ErrorCategory.AUTHENTICATION
        pipecats: ErrorCategory | None = super()._classify_error(exception)  # type: ignore[misc]
        return pipecats


def _names_a_key(exception: Exception) -> bool:
    """A WebSocket handshake's body is bytes on its response; the OpenAI
    SDK's is the parsed JSON on the exception itself."""
    handshake = getattr(getattr(exception, "response", None), "body", None)
    if isinstance(handshake, bytes):
        return b"API key" in handshake
    return "API key" in str(getattr(exception, "body", ""))
