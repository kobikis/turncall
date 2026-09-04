"""Tests for webhook HMAC-SHA256 signing and verification."""

import time

import pytest

from turncall.events.webhook_signing import sign_payload, verify_signature


@pytest.mark.unit
class TestWebhookSigning:
    def test_sign_produces_v1_prefix(self) -> None:
        sig, _ts = sign_payload('{"event": "test"}', "secret123")
        assert sig.startswith("v1=")

    def test_sign_is_deterministic(self) -> None:
        payload = '{"event": "test"}'
        sig1, _ = sign_payload(payload, "secret", timestamp=1000)
        sig2, _ = sign_payload(payload, "secret", timestamp=1000)
        assert sig1 == sig2

    def test_different_secrets_different_sigs(self) -> None:
        payload = '{"event": "test"}'
        sig1, _ = sign_payload(payload, "secret1", timestamp=1000)
        sig2, _ = sign_payload(payload, "secret2", timestamp=1000)
        assert sig1 != sig2

    def test_different_payloads_different_sigs(self) -> None:
        sig1, _ = sign_payload("payload1", "secret", timestamp=1000)
        sig2, _ = sign_payload("payload2", "secret", timestamp=1000)
        assert sig1 != sig2

    def test_verify_valid_signature(self) -> None:
        payload = '{"event": "call.ended"}'
        secret = "test-secret"
        sig, ts = sign_payload(payload, secret)
        assert verify_signature(payload, secret, sig, ts)

    def test_verify_rejects_wrong_secret(self) -> None:
        payload = '{"test": true}'
        sig, ts = sign_payload(payload, "correct-secret")
        assert not verify_signature(payload, "wrong-secret", sig, ts)

    def test_verify_rejects_tampered_payload(self) -> None:
        payload = '{"original": true}'
        secret = "secret"
        sig, ts = sign_payload(payload, secret)
        assert not verify_signature('{"tampered": true}', secret, sig, ts)

    def test_verify_rejects_expired_timestamp(self) -> None:
        payload = "test"
        secret = "secret"
        old_ts = int(time.time()) - 600
        sig, _ = sign_payload(payload, secret, timestamp=old_ts)
        assert not verify_signature(payload, secret, sig, old_ts, max_age_seconds=300)

    def test_verify_accepts_recent_timestamp(self) -> None:
        payload = "test"
        secret = "secret"
        sig, ts = sign_payload(payload, secret)
        assert verify_signature(payload, secret, sig, ts, max_age_seconds=300)
