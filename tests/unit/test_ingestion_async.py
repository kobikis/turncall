"""The async ingestion split (review: upload ran extract/chunk/embed inline).
Upload now creates a 'processing' row + backgrounds the heavy work."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.services import document_ingestion as di


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_pending_document_stores_and_creates_row():
    session = MagicMock()
    session.flush = AsyncMock()
    storage = MagicMock()
    storage.upload = AsyncMock()
    row = SimpleNamespace(id=uuid.uuid4())
    with patch.object(di.knowledge_repo, "create_document", new=AsyncMock(return_value=row)) as cd:
        out = await di.create_pending_document(
            session, storage,
            knowledge_base_id=uuid.uuid4(), filename="f.txt",
            content_type="text/plain", data=b"hello",
        )
    assert out is row
    storage.upload.assert_awaited_once()  # file stored
    cd.assert_awaited_once()              # row created (status 'processing')


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pending_empty_text_marks_failed():
    session = MagicMock()
    doc_id = uuid.uuid4()
    with patch.object(di.knowledge_repo, "update_document_status", new=AsyncMock()) as upd:
        result = await di.process_pending_document(
            session, doc_id,
            knowledge_base_id=uuid.uuid4(), filename="f.txt",
            content_type="text/plain", data=b"   ",  # whitespace -> no text
        )
    assert result.status == "failed"
    assert upd.await_args.kwargs["status"] == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_in_background_runs_process_and_commits():
    session = MagicMock()
    session.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    doc_id = uuid.uuid4()

    with patch.object(di, "process_pending_document", new=AsyncMock()) as proc:
        di.ingest_in_background(
            factory,
            document_id=doc_id, knowledge_base_id=uuid.uuid4(),
            filename="f.txt", content_type="text/plain", data=b"hi",
            openai_api_key="k", embedding_model="text-embedding-3-small",
            chunk_size=512, chunk_overlap=64,
        )
        # let the spawned task run
        assert di._INGEST_TASKS  # strong ref held
        await next(iter(di._INGEST_TASKS))

    proc.assert_awaited_once()
    assert proc.await_args.args[1] == doc_id  # processed the right doc
    session.commit.assert_awaited_once()
