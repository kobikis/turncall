"""Tests for API key generation and hashing."""

import pytest

from turncall.auth.api_keys import (
    API_KEY_PREFIX,
    generate_api_key,
    hash_api_key,
)


@pytest.mark.unit
class TestApiKeyGeneration:
    def test_generate_produces_prefixed_key(self) -> None:
        result = generate_api_key()
        assert result.raw_key.startswith(API_KEY_PREFIX)

    def test_generate_produces_12_char_prefix(self) -> None:
        result = generate_api_key()
        assert len(result.key_prefix) == 12

    def test_prefix_matches_raw_key_start(self) -> None:
        result = generate_api_key()
        assert result.raw_key[:12] == result.key_prefix

    def test_hash_is_deterministic(self) -> None:
        key = "tc_test_key_12345"
        assert hash_api_key(key) == hash_api_key(key)

    def test_hash_differs_for_different_keys(self) -> None:
        key1 = generate_api_key()
        key2 = generate_api_key()
        assert key1.key_hash != key2.key_hash

    def test_generated_hash_matches_manual_hash(self) -> None:
        result = generate_api_key()
        assert result.key_hash == hash_api_key(result.raw_key)

    def test_hash_is_64_char_hex(self) -> None:
        result = generate_api_key()
        assert len(result.key_hash) == 64
        int(result.key_hash, 16)  # Should not raise

    def test_two_generations_produce_different_keys(self) -> None:
        key1 = generate_api_key()
        key2 = generate_api_key()
        assert key1.raw_key != key2.raw_key
