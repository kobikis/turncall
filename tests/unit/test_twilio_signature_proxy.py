"""Twilio signature validation must use the public (forwarded) URL.

Behind a tunnel/proxy the container sees http + an internal host, but Twilio
signs the https public URL. _public_url must reconstruct the signed URL from
X-Forwarded-Proto / Host so the HMAC matches.
"""

from starlette.requests import Request
from twilio.request_validator import RequestValidator

from turncall.webhooks.twilio_handlers import _public_url
from turncall.webhooks.twilio_signature import validate_twilio_signature

AUTH_TOKEN = "test_auth_token_123"
PUBLIC_HOST = "abc123.ngrok.io"
PATH = "/webhooks/twilio/voice/inbound"
PARAMS = {"CallSid": "CA123", "From": "+15550001111", "To": "+15550002222"}


def _request(headers: dict[str, str]) -> Request:
    # Container receives plain http on an internal host/port.
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "query_string": b"",
        "server": ("172.18.0.5", 8090),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_public_url_uses_forwarded_proto_and_host() -> None:
    req = _request({"host": PUBLIC_HOST, "x-forwarded-proto": "https"})
    assert _public_url(req) == f"https://{PUBLIC_HOST}{PATH}"


def test_forwarded_proto_chain_takes_first() -> None:
    req = _request({"host": PUBLIC_HOST, "x-forwarded-proto": "https,http"})
    assert _public_url(req).startswith("https://")


def test_signature_validates_through_proxy() -> None:
    # Twilio signs the public https URL.
    public_url = f"https://{PUBLIC_HOST}{PATH}"
    signature = RequestValidator(AUTH_TOKEN).compute_signature(public_url, PARAMS)

    # Reconstructed URL matches what Twilio signed -> valid.
    req = _request({"host": PUBLIC_HOST, "x-forwarded-proto": "https"})
    assert validate_twilio_signature(AUTH_TOKEN, signature, _public_url(req), PARAMS)

    # The old behaviour (raw http container URL) would NOT match.
    internal_url = "http://172.18.0.5:8090" + PATH
    assert not validate_twilio_signature(AUTH_TOKEN, signature, internal_url, PARAMS)


if __name__ == "__main__":
    test_public_url_uses_forwarded_proto_and_host()
    test_forwarded_proto_chain_takes_first()
    test_signature_validates_through_proxy()
    print("ok")
