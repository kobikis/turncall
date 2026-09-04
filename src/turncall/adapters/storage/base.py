"""Abstract object storage adapter interface."""

from abc import ABC, abstractmethod


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
