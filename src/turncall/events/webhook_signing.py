"""HMAC-SHA256 webhook signing and verification."""

import hashlib
import hmac
import time


def sign_payload(
    payload: str, secret: str, timestamp: int | None = None
) -> tuple[str, int]:
    """Sign a webhook payload with HMAC-SHA256.

    Returns (signature, timestamp) tuple. The signature format is:
    v1=<hex_hmac> where the HMAC is computed over "timestamp.payload".
    """
    ts = timestamp or int(time.time())
    signed_content = f"{ts}.{payload}"
    signature = hmac.new(
        secret.encode(),
        signed_content.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={signature}", ts


def verify_signature(
    payload: str,
    secret: str,
    signature: str,
    timestamp: int,
    *,
    max_age_seconds: int = 300,
) -> bool:
    """Verify a webhook signature.

    Checks HMAC validity and that the timestamp is within max_age_seconds.
    """
    now = int(time.time())
    if abs(now - timestamp) > max_age_seconds:
        return False

    expected_sig, _ = sign_payload(payload, secret, timestamp)
    return hmac.compare_digest(signature, expected_sig)
