"""Abstract object storage adapter interface."""

from abc import ABC, abstractmethod
from collections.abc import Iterable


class ObjectStorageAdapter(ABC):
    """Interface for object storage (S3, local filesystem)."""

    @abstractmethod
    async def upload(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload data and return the storage URL/path."""

    @abstractmethod
    async def download(self, key: str) -> bytes:
        """Download data by key."""

    @abstractmethod
    async def get_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Get a presigned/temporary URL for accessing the object."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete an object by key."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if an object exists."""

    async def delete_many(self, keys: Iterable[str]) -> dict[str, Exception]:
        """Delete every key, each failure kept rather than raised: the keys
        that could not be deleted, with why. A key already gone is deleted.
        One at a time here; a backend with a batch delete overrides it."""
        failed: dict[str, Exception] = {}
        for key in keys:
            try:
                await self.delete(key)
            except Exception as exc:
                failed[key] = exc
        return failed
