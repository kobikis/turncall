"""Object storage adapter (S3, local filesystem)."""

from collections.abc import Iterable

from loguru import logger

from turncall.adapters.storage.base import ObjectStorageAdapter
from turncall.adapters.storage.local import LocalStorageAdapter
from turncall.adapters.storage.s3 import S3StorageAdapter

__all__ = [
    "LocalStorageAdapter",
    "ObjectStorageAdapter",
    "S3StorageAdapter",
    "delete_objects",
]


def create_storage_adapter(
    backend: str = "local",
    *,
    local_path: str = "./storage",
    s3_bucket: str = "",
    aws_region: str = "us-east-1",
) -> ObjectStorageAdapter:
    """Create a storage adapter based on configuration."""
    if backend == "s3":
        if not s3_bucket:
            msg = "S3_BUCKET_NAME is required when STORAGE_BACKEND=s3"
            raise ValueError(msg)
        return S3StorageAdapter(bucket=s3_bucket, region=aws_region)
    return LocalStorageAdapter(base_path=local_path)


async def delete_objects(storage: ObjectStorageAdapter, keys: Iterable[str]) -> None:
    """Delete each key, best-effort (#168): a failure is logged, never raised.
    Losing an object is bad; failing a delete or a purge because one is already
    gone is worse. Run it *after* the commit, so a rolled-back delete keeps its
    object."""
    for key in keys:
        try:
            await storage.delete(key)
        except Exception as exc:
            logger.warning("storage_orphan key={key}: {err}", key=key, err=exc)
