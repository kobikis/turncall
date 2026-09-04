"""Local filesystem storage adapter."""

from pathlib import Path

import aiofiles
import aiofiles.os

from turncall.adapters.storage.base import ObjectStorageAdapter


class LocalStorageAdapter(ObjectStorageAdapter):
    """Store objects on the local filesystem."""

    def __init__(self, base_path: str = "./storage") -> None:
        self._base_path = Path(base_path)

    def _resolve(self, key: str) -> Path:
        return self._base_path / key

    async def upload(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> str:
        path = self._resolve(key)
        await aiofiles.os.makedirs(str(path.parent), exist_ok=True)
        async with aiofiles.open(str(path), "wb") as f:
            await f.write(data)
        return str(path)

    async def download(self, key: str) -> bytes:
        path = self._resolve(key)
        async with aiofiles.open(str(path), "rb") as f:
            return await f.read()

    async def get_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        # Local storage has no presigned URLs; return file path
        return str(self._resolve(key))

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        if await aiofiles.os.path.exists(str(path)):
            await aiofiles.os.remove(str(path))

    async def exists(self, key: str) -> bool:
        return await aiofiles.os.path.exists(str(self._resolve(key)))
