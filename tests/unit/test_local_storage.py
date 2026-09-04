"""Tests for local filesystem storage adapter."""

import pytest

from turncall.adapters.storage.local import LocalStorageAdapter


@pytest.mark.unit
class TestLocalStorageAdapter:
    @pytest.fixture
    def storage(self, tmp_path: object) -> LocalStorageAdapter:
        return LocalStorageAdapter(base_path=str(tmp_path))

    @pytest.mark.asyncio
    async def test_upload_and_download(self, storage: LocalStorageAdapter) -> None:
        data = b"hello world"
        path = await storage.upload("test/file.txt", data, content_type="text/plain")
        assert path.endswith("test/file.txt")

        downloaded = await storage.download("test/file.txt")
        assert downloaded == data

    @pytest.mark.asyncio
    async def test_exists_true(self, storage: LocalStorageAdapter) -> None:
        await storage.upload("exists.txt", b"data")
        assert await storage.exists("exists.txt") is True

    @pytest.mark.asyncio
    async def test_exists_false(self, storage: LocalStorageAdapter) -> None:
        assert await storage.exists("nonexistent.txt") is False

    @pytest.mark.asyncio
    async def test_delete(self, storage: LocalStorageAdapter) -> None:
        await storage.upload("to_delete.txt", b"data")
        assert await storage.exists("to_delete.txt") is True

        await storage.delete("to_delete.txt")
        assert await storage.exists("to_delete.txt") is False

    @pytest.mark.asyncio
    async def test_delete_nonexistent_no_error(
        self, storage: LocalStorageAdapter
    ) -> None:
        # Should not raise
        await storage.delete("does_not_exist.txt")

    @pytest.mark.asyncio
    async def test_upload_creates_subdirectories(
        self, storage: LocalStorageAdapter
    ) -> None:
        await storage.upload("a/b/c/deep.txt", b"nested")
        result = await storage.download("a/b/c/deep.txt")
        assert result == b"nested"

    @pytest.mark.asyncio
    async def test_presigned_url_returns_path(
        self, storage: LocalStorageAdapter
    ) -> None:
        await storage.upload("file.txt", b"data")
        url = await storage.get_presigned_url("file.txt")
        assert "file.txt" in url

    @pytest.mark.asyncio
    async def test_upload_binary_content(self, storage: LocalStorageAdapter) -> None:
        data = bytes(range(256))
        await storage.upload(
            "binary.bin", data, content_type="application/octet-stream"
        )
        downloaded = await storage.download("binary.bin")
        assert downloaded == data

    @pytest.mark.asyncio
    async def test_overwrite_existing_file(self, storage: LocalStorageAdapter) -> None:
        await storage.upload("file.txt", b"original")
        await storage.upload("file.txt", b"updated")
        result = await storage.download("file.txt")
        assert result == b"updated"
