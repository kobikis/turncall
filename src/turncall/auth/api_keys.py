"""API key generation, hashing, and validation.

Keys are stored as a keyed HMAC-SHA256 (peppered with API_KEY_HASH_SECRET), not
a bare SHA-256 — a database leak alone can't brute-force keys without the
secret. The hash is still deterministic (a function of the raw key + the
process-wide secret), so lookup-by-hash is unchanged. `legacy_hash_api_key` is
the pre-pepper scheme, kept only so auth can dual-read + upgrade old keys on use
(raw keys aren't stored, so they can't be backfilled).
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

API_KEY_PREFIX = "tc_"
API_KEY_BYTE_LENGTH = 32


@dataclass(frozen=True)
class GeneratedApiKey:
    """Result of generating a new API key."""

    raw_key: str
    key_prefix: str
    key_hash: str


def generate_api_key() -> GeneratedApiKey:
    """Generate a new API key with prefix, raw value, and hash."""
    random_part = secrets.token_urlsafe(API_KEY_BYTE_LENGTH)
    raw_key = f"{API_KEY_PREFIX}{random_part}"
    key_prefix = raw_key[:12]
    key_hash = hash_api_key(raw_key)
    return GeneratedApiKey(
        raw_key=raw_key,
        key_prefix=key_prefix,
        key_hash=key_hash,
    )


def hash_api_key(raw_key: str) -> str:
    """Peppered HMAC-SHA256 of the raw key (keyed with API_KEY_HASH_SECRET)."""
    from turncall.config import get_settings

    secret = get_settings().auth.api_key_hash_secret
    return hmac.new(secret.encode(), raw_key.encode(), hashlib.sha256).hexdigest()


def legacy_hash_api_key(raw_key: str) -> str:
    """Pre-pepper unsalted SHA-256. Kept only for dual-read during rollover —
    do not use for new keys."""
    return hashlib.sha256(raw_key.encode()).hexdigest()
