"""The media token: proof that a carrier media stream is the call's own.

A call id is not a secret — it rides in webhooks, call-init, the API and logs
— so a media route that trusts one hands the call to whoever presents it: the
real pipeline built on their socket, the caller's agent fed their audio and
its tools driven by it. Each call's stream setup (Twilio's TwiML
`<Parameter>`, Telnyx's `stream_url`, Asterisk's external-media channel id)
therefore also carries this token, which only that setup ever hands out, and
the shared media entry point refuses a stream without it.

It is an HMAC of the call id, so nothing is stored and any replica checks a
token another minted. The key is `API_KEY_HASH_SECRET`: a server secret every
replica already shares and production already sets, used here under its own
label so the two uses cannot stand in for each other.
"""

from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

from turncall.config import get_settings

_LABEL = b"turncall-media-stream:v1:"


def media_token(call_id: UUID) -> str:
    """The token a carrier presents to open `call_id`'s media stream."""
    key = get_settings().auth.api_key_hash_secret.encode()
    return hmac.new(key, _LABEL + str(call_id).encode(), hashlib.sha256).hexdigest()


def media_token_valid(call_id: UUID, token: str) -> bool:
    """Whether `token` is `call_id`'s, compared in constant time."""
    if not token:
        return False
    return hmac.compare_digest(media_token(call_id).encode(), token.encode())
