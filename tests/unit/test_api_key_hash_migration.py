"""API key hashing: peppered HMAC + legacy dual-read/upgrade-on-use."""

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.auth import api_keys
from turncall.auth import dependencies as dep


@pytest.mark.unit
def test_hash_is_peppered_hmac_not_bare_sha256():
    raw = "tc_example"
    assert api_keys.hash_api_key(raw) != hashlib.sha256(raw.encode()).hexdigest()
    assert api_keys.legacy_hash_api_key(raw) == hashlib.sha256(raw.encode()).hexdigest()


@pytest.mark.unit
def test_hash_depends_on_the_secret():
    raw = "tc_example"
    from turncall.config import get_settings

    s = get_settings()
    original = s.auth.api_key_hash_secret
    try:
        s.auth.api_key_hash_secret = "secret-a"
        a = api_keys.hash_api_key(raw)
        s.auth.api_key_hash_secret = "secret-b"
        b = api_keys.hash_api_key(raw)
    finally:
        s.auth.api_key_hash_secret = original
    assert a != b  # rotating the pepper changes the hash


def _key_row(raw):
    return MagicMock(
        project_id=uuid.uuid4(),
        id=uuid.uuid4(),
        role="admin",
        revoked_at=None,
        environment="production",
        key_prefix=raw[:12],
        key_hash=api_keys.legacy_hash_api_key(raw),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_key_authenticates_and_upgrades_in_place():
    raw = "tc_legacykey123"
    row = _key_row(raw)
    session = AsyncMock()
    with (
        # HMAC lookup misses, legacy lookup hits
        patch.object(
            dep.api_key_repo, "get_api_key_by_hash", new=AsyncMock(side_effect=[None, row])
        ),
        patch.object(dep.project_repo, "get_project_by_id", new=AsyncMock(return_value=object())),
    ):
        ctx = await dep.resolve_auth_context(session, authorization=f"Bearer {raw}")
    assert ctx.project_id == row.project_id
    assert row.key_hash == api_keys.hash_api_key(raw)  # upgraded to peppered hash
    session.flush.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_peppered_key_authenticates_without_upgrade():
    raw = "tc_newkey456"
    row = _key_row(raw)
    row.key_hash = api_keys.hash_api_key(raw)
    session = AsyncMock()
    with (
        patch.object(dep.api_key_repo, "get_api_key_by_hash", new=AsyncMock(return_value=row)),
        patch.object(dep.project_repo, "get_project_by_id", new=AsyncMock(return_value=object())),
    ):
        await dep.resolve_auth_context(session, authorization=f"Bearer {raw}")
    session.flush.assert_not_called()  # already peppered — no upgrade
