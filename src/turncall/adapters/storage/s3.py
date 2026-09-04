"""AWS S3 storage adapter (async via aioboto3)."""

import aioboto3
from botocore.exceptions import ClientError

from turncall.adapters.storage.base import ObjectStorageAdapter


class S3StorageAdapter(ObjectStorageAdapter):
    """Store objects in AWS S3 using async I/O."""

    def __init__(self, bucket: str, region: str = "us-east-1") -> None:
        self._bucket = bucket
        self._region = region
        self._session = aioboto3.Session()

    async def upload(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> str:
        async with self._session.client("s3", region_name=self._region) as s3:
            await s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return f"s3://{self._bucket}/{key}"

    async def download(self, key: str) -> bytes:
        async with self._session.client("s3", region_name=self._region) as s3:
            response = await s3.get_object(Bucket=self._bucket, Key=key)
            async with response["Body"] as stream:
                return await stream.read()

    async def get_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        async with self._session.client("s3", region_name=self._region) as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )

    async def delete(self, key: str) -> None:
        async with self._session.client("s3", region_name=self._region) as s3:
            await s3.delete_object(Bucket=self._bucket, Key=key)

    async def exists(self, key: str) -> bool:
        try:
            async with self._session.client("s3", region_name=self._region) as s3:
                await s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            # Only 404/NoSuchKey means "absent". A 403 or throttle must not be
            # silently reported as not-found — re-raise so callers see the error.
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True
