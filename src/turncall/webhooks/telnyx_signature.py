"""Telnyx webhook signatures: Ed25519 over `{timestamp}|{raw body}`.

Telnyx signs every webhook with its account's private key and sends the
signature (`telnyx-signature-ed25519`, base64) and the signing time
(`telnyx-timestamp`, Unix seconds). The account's public key verifies it. The
scheme is the one Telnyx's own SDKs implement, including their five-minute
window: a captured webhook replayed later is refused even with its signature
intact.
"""

from __future__ import annotations

import base64
import binascii
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SIGNATURE_HEADER = "telnyx-signature-ed25519"
TIMESTAMP_HEADER = "telnyx-timestamp"
TOLERANCE_SECONDS = 300


def verify_telnyx_signature(
    public_key: str,
    signature: str,
    timestamp: str,
    body: bytes,
    *,
    now: float | None = None,
) -> bool:
    """Whether `body` was signed by the holder of `public_key` (base64) at
    `timestamp`, within the replay window. Any malformed input is False."""
    if not (public_key and signature and timestamp):
        return False
    try:
        signed_at = int(timestamp)
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
        raw_signature = base64.b64decode(signature)
    except (ValueError, binascii.Error):
        return False
    current = time.time() if now is None else now
    if abs(current - signed_at) > TOLERANCE_SECONDS:
        return False
    try:
        key.verify(raw_signature, timestamp.encode() + b"|" + body)
    except InvalidSignature:
        return False
    return True
