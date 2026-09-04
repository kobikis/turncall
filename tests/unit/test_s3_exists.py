"""S3StorageAdapter.exists: 404 -> False, other ClientErrors re-raise
(review: swallowing 403/throttle as not-found)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from turncall.adapters.storage.s3 import S3StorageAdapter


def _client_raising(code: str):
    err = ClientError({"Error": {"Code": code}}, "HeadObject")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(head_object=AsyncMock(side_effect=err)))
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.unit
@pytest.mark.asyncio
async def test_404_is_false():
    a = S3StorageAdapter("b")
    with patch.object(a._session, "client", return_value=_client_raising("404")):
        assert await a.exists("k") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_403_reraises():
    a = S3StorageAdapter("b")
    with patch.object(a._session, "client", return_value=_client_raising("403")):
        with pytest.raises(ClientError):
            await a.exists("k")
