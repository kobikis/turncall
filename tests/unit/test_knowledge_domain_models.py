"""Tests for knowledge base domain models (immutability, defaults, serialization)."""

import uuid
from datetime import UTC, datetime

import pytest

from turncall.domain.enums import DocumentStatus, KnowledgeRetrievalMode
from turncall.domain.models import (
    AgentConfig,
    Document,
    DocumentChunk,
    KnowledgeBase,
    KnowledgeBaseAttachment,
)


@pytest.mark.unit
class TestKnowledgeBaseAttachment:
    def test_defaults(self) -> None:
        att = KnowledgeBaseAttachment(knowledge_base_id="kb-123")
        assert att.mode == KnowledgeRetrievalMode.AUTO
        assert att.priority == 0
        assert att.top_k == 5
        assert att.similarity_threshold == 0.3
        assert att.tool_description is None

    def test_frozen(self) -> None:
        att = KnowledgeBaseAttachment(knowledge_base_id="kb-123")
        with pytest.raises(Exception):
            att.mode = KnowledgeRetrievalMode.TOOL  # type: ignore[misc]

    def test_tool_mode(self) -> None:
        att = KnowledgeBaseAttachment(
            knowledge_base_id="kb-123",
            mode=KnowledgeRetrievalMode.TOOL,
            tool_description="Search docs",
        )
        assert att.mode == KnowledgeRetrievalMode.TOOL
        assert att.tool_description == "Search docs"

    def test_serialization_roundtrip(self) -> None:
        att = KnowledgeBaseAttachment(
            knowledge_base_id="kb-456",
            mode=KnowledgeRetrievalMode.PROMPT,
            priority=2,
            top_k=10,
            similarity_threshold=0.5,
        )
        data = att.model_dump()
        restored = KnowledgeBaseAttachment.model_validate(data)
        assert restored.knowledge_base_id == "kb-456"
        assert restored.mode == KnowledgeRetrievalMode.PROMPT
        assert restored.top_k == 10


@pytest.mark.unit
class TestAgentConfigNoLongerCarriesAttachments:
    """`AgentConfig.knowledge_bases` was vestigial: attachments live in the
    agent_knowledge_bases table and are loaded by load_agent_kb_attachments, and
    the API schema forbade the field outright, so nothing could ever set it."""

    def test_the_field_is_gone(self) -> None:
        assert "knowledge_bases" not in AgentConfig.model_fields

    def test_a_stored_config_carrying_the_old_key_still_loads(self) -> None:
        """Pydantic ignores unknown keys, so a config_blob written while the
        field existed is not a migration problem. Worth pinning rather than
        assuming — this is stored customer data."""
        config = AgentConfig.model_validate(
            {
                "system_prompt": "hello",
                "knowledge_bases": [{"knowledge_base_id": "kb-1", "top_k": 3}],
            }
        )

        assert config.system_prompt == "hello"

    def test_the_attachment_model_itself_survives(self) -> None:
        """Retrieval still uses it — only the AgentConfig field went."""
        att = KnowledgeBaseAttachment(knowledge_base_id="kb-1", mode="prompt")

        assert att.mode == KnowledgeRetrievalMode.PROMPT


@pytest.mark.unit
class TestKnowledgeBaseDomainModel:
    def test_frozen(self) -> None:
        kb = KnowledgeBase(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="test-kb",
            embedding_model="text-embedding-3-small",
            chunk_size=512,
            chunk_overlap=64,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            kb.name = "mutated"  # type: ignore[misc]

    def test_defaults(self) -> None:
        kb = KnowledgeBase(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="kb",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert kb.embedding_model == "text-embedding-3-small"
        assert kb.chunk_size == 512
        assert kb.chunk_overlap == 64
        assert kb.description is None


@pytest.mark.unit
class TestDocumentDomainModel:
    def test_frozen(self) -> None:
        doc = Document(
            id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            filename="test.pdf",
            content_type="application/pdf",
            storage_key="kb/123/docs/test.pdf",
            char_count=1000,
            chunk_count=5,
            status=DocumentStatus.READY,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            doc.status = DocumentStatus.FAILED  # type: ignore[misc]


@pytest.mark.unit
class TestDocumentChunkDomainModel:
    def test_frozen(self) -> None:
        chunk = DocumentChunk(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            chunk_index=0,
            content="Some text",
            token_count=3,
            created_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            chunk.content = "mutated"  # type: ignore[misc]


@pytest.mark.unit
class TestKnowledgeRetrievalModeEnum:
    def test_values(self) -> None:
        assert KnowledgeRetrievalMode.AUTO == "auto"
        assert KnowledgeRetrievalMode.TOOL == "tool"
        assert KnowledgeRetrievalMode.PROMPT == "prompt"

    def test_string_equality(self) -> None:
        assert KnowledgeRetrievalMode.AUTO == "auto"
        assert KnowledgeRetrievalMode("tool") == KnowledgeRetrievalMode.TOOL


@pytest.mark.unit
class TestDocumentStatusEnum:
    def test_values(self) -> None:
        assert DocumentStatus.PROCESSING == "processing"
        assert DocumentStatus.READY == "ready"
        assert DocumentStatus.FAILED == "failed"
