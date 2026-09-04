"""WhatsApp webhook signature validation (HMAC-SHA256)."""

import hashlib
import hmac


def validate_whatsapp_signature(
    secret: str,
    raw_body: bytes,
    signature_header: str,
) -> bool:
    """Validate X-Hub-Signature-256 header using HMAC-SHA256.

    Args:
        secret: WhatsApp App Secret
        raw_body: Raw request body bytes
        signature_header: Value of X-Hub-Signature-256 header (e.g. "sha256=abc...")

    Returns:
        True if signature is valid, False otherwise.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    received_sig = signature_header[7:]  # Strip "sha256=" prefix

    computed_sig = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed_sig, received_sig)
