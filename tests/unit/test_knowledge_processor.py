"""Tests for knowledge processor constants and ingestion result.

Note: KnowledgeRetrievalProcessor itself requires Pipecat runtime,
so we test it indirectly. These tests cover the schema, constants,
and IngestionResult dataclass.
"""

import uuid

import pytest


@pytest.mark.unit
class TestKnowledgeToolSchema:
    def test_schema_has_required_fields(self) -> None:
        # Import inline to avoid pulling in pipecat at module level
        from turncall.orchestrator.knowledge_processor import KNOWLEDGE_TOOL_SCHEMA

        assert KNOWLEDGE_TOOL_SCHEMA["name"] == "query_knowledge"
        assert "description" in KNOWLEDGE_TOOL_SCHEMA
        assert "parameters" in KNOWLEDGE_TOOL_SCHEMA

    def test_schema_parameters_valid(self) -> None:
        from turncall.orchestrator.knowledge_processor import KNOWLEDGE_TOOL_SCHEMA

        params = KNOWLEDGE_TOOL_SCHEMA["parameters"]
        assert params["type"] == "object"
        assert "query" in params["properties"]
        assert params["properties"]["query"]["type"] == "string"
        assert "query" in params["required"]

    def test_schema_description_nonempty(self) -> None:
        from turncall.orchestrator.knowledge_processor import KNOWLEDGE_TOOL_SCHEMA

        assert len(KNOWLEDGE_TOOL_SCHEMA["description"]) > 10


@pytest.mark.unit
class TestIngestionResult:
    def test_frozen(self) -> None:
        from turncall.services.document_ingestion import IngestionResult

        result = IngestionResult(
            document_id=uuid.uuid4(),
            char_count=100,
            chunk_count=5,
            status="ready",
        )
        with pytest.raises(Exception):
            result.status = "failed"  # type: ignore[misc]

    def test_success_result(self) -> None:
        from turncall.services.document_ingestion import IngestionResult

        doc_id = uuid.uuid4()
        result = IngestionResult(
            document_id=doc_id,
            char_count=5000,
            chunk_count=10,
            status="ready",
        )
        assert result.document_id == doc_id
        assert result.error is None

    def test_failure_result(self) -> None:
        from turncall.services.document_ingestion import IngestionResult

        result = IngestionResult(
            document_id=uuid.uuid4(),
            char_count=0,
            chunk_count=0,
            status="failed",
            error="No text could be extracted",
        )
        assert result.status == "failed"
        assert "No text" in result.error


@pytest.mark.unit
class TestEmbeddingModelValidation:
    def test_supported_models(self) -> None:
        from turncall.services.document_ingestion import SUPPORTED_EMBEDDING_MODELS

        assert "text-embedding-3-small" in SUPPORTED_EMBEDDING_MODELS
        assert "text-embedding-3-large" in SUPPORTED_EMBEDDING_MODELS
        assert "text-embedding-ada-002" in SUPPORTED_EMBEDDING_MODELS

    def test_unsupported_model_not_in_set(self) -> None:
        from turncall.services.document_ingestion import SUPPORTED_EMBEDDING_MODELS

        assert "gpt-4o" not in SUPPORTED_EMBEDDING_MODELS


@pytest.mark.unit
class TestMaxExtractedChars:
    def test_limit_exists(self) -> None:
        from turncall.services.document_ingestion import MAX_EXTRACTED_CHARS

        assert MAX_EXTRACTED_CHARS == 1_000_000


@pytest.mark.unit
class TestAllowedContentTypes:
    def test_common_types_allowed(self) -> None:
        from turncall.api.v1.knowledge import ALLOWED_CONTENT_TYPES

        assert "text/plain" in ALLOWED_CONTENT_TYPES
        assert "application/pdf" in ALLOWED_CONTENT_TYPES
        assert "text/markdown" in ALLOWED_CONTENT_TYPES
        assert "application/json" in ALLOWED_CONTENT_TYPES

    def test_dangerous_types_not_allowed(self) -> None:
        from turncall.api.v1.knowledge import ALLOWED_CONTENT_TYPES

        assert "application/x-executable" not in ALLOWED_CONTENT_TYPES
        assert "application/octet-stream" not in ALLOWED_CONTENT_TYPES
        assert "text/html" not in ALLOWED_CONTENT_TYPES


@pytest.mark.unit
@pytest.mark.asyncio
class TestLoadAgentKbAttachments:
    async def test_maps_rows_to_pipeline_dicts(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        from turncall.orchestrator.knowledge_processor import (
            load_agent_kb_attachments,
        )

        kb_id = uuid.uuid4()

        class _Row:
            knowledge_base_id = kb_id
            mode = "auto"
            priority = 0
            top_k = 7
            similarity_threshold = 0.4
            tool_description = None

        @asynccontextmanager
        async def _factory():
            yield AsyncMock()

        with patch(
            "turncall.storage.repositories.knowledge_repo.get_agent_kb_attachments",
            new=AsyncMock(
                return_value=[
                    {
                        "knowledge_base_id": kb_id,
                        "mode": "auto",
                        "priority": 0,
                        "top_k": 7,
                        "similarity_threshold": 0.4,
                        "tool_description": None,
                    }
                ]
            ),
        ):
            out = await load_agent_kb_attachments(_factory, uuid.uuid4())
        assert out == [
            {
                "knowledge_base_id": kb_id,
                "mode": "auto",
                "priority": 0,
                "top_k": 7,
                "similarity_threshold": 0.4,
                "tool_description": None,
            }
        ]

    async def test_returns_empty_on_error(self) -> None:
        from turncall.orchestrator.knowledge_processor import (
            load_agent_kb_attachments,
        )

        def _broken_factory():
            raise RuntimeError("db down")

        out = await load_agent_kb_attachments(_broken_factory, uuid.uuid4())
        assert out == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_processor_calls_super_process_frame() -> None:
    """Regression: the processor MUST delegate to FrameProcessor.process_frame
    (Pipecat 1.4 lifecycle) or it silently drops every frame and stalls."""
    from unittest.mock import AsyncMock, patch

    from pipecat.processors.frame_processor import (
        FrameDirection,
        FrameProcessor,
    )

    from turncall.orchestrator.knowledge_processor import (
        KnowledgeRetrievalProcessor,
    )

    p = KnowledgeRetrievalProcessor(
        knowledge_base_ids=[uuid.uuid4()],
        session_factory=AsyncMock(),
        openai_api_key="x",
    )
    p.push_frame = AsyncMock()

    with patch.object(
        FrameProcessor, "process_frame", new=AsyncMock()
    ) as base_pf:
        # A plain object stands in for a frame we don't branch on; the point is
        # that the base method is awaited regardless of frame type.
        await p.process_frame(object(), FrameDirection.DOWNSTREAM)

    base_pf.assert_awaited_once()
