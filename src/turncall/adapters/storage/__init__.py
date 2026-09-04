"""Object storage adapter (S3, local filesystem)."""

from turncall.adapters.storage.base import ObjectStorageAdapter
from turncall.adapters.storage.local import LocalStorageAdapter
from turncall.adapters.storage.s3 import S3StorageAdapter

__all__ = ["LocalStorageAdapter", "ObjectStorageAdapter", "S3StorageAdapter"]


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
