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

KB_ID = uuid.uuid4()


@pytest.mark.unit
class TestKnowledgeBaseAttachment:
    def test_defaults(self) -> None:
        att = KnowledgeBaseAttachment(knowledge_base_id=KB_ID)
        assert att.mode == KnowledgeRetrievalMode.AUTO
        assert att.priority == 0
        assert att.top_k == 5
        assert att.similarity_threshold == 0.3
        assert att.tool_description is None

    def test_frozen(self) -> None:
        att = KnowledgeBaseAttachment(knowledge_base_id=KB_ID)
        with pytest.raises(Exception):
            att.mode = KnowledgeRetrievalMode.TOOL  # type: ignore[misc]

    def test_tool_mode(self) -> None:
        att = KnowledgeBaseAttachment(
            knowledge_base_id=KB_ID,
            mode=KnowledgeRetrievalMode.TOOL,
            tool_description="Search docs",
        )
        assert att.mode == KnowledgeRetrievalMode.TOOL
        assert att.tool_description == "Search docs"

    def test_serialization_roundtrip(self) -> None:
        att = KnowledgeBaseAttachment(
            knowledge_base_id=KB_ID,
            mode=KnowledgeRetrievalMode.PROMPT,
            priority=2,
            top_k=10,
            similarity_threshold=0.5,
        )
        data = att.model_dump()
        restored = KnowledgeBaseAttachment.model_validate(data)
        assert restored.knowledge_base_id == KB_ID
        assert restored.mode == KnowledgeRetrievalMode.PROMPT
        assert restored.top_k == 10


@pytest.mark.unit
class TestAgentConfigAttachments:
    """`AgentConfig.knowledge_bases` names an inline agent's KBs (#260). A
    stored agent's live in agent_knowledge_bases and are loaded by
    load_agent_kb_attachments; the agent endpoints refuse the field."""

    def test_a_stored_config_carrying_the_old_key_still_loads(self) -> None:
        """A config_blob from when the field was vestigial may carry ids that
        were never UUIDs. That entry is skipped, the config still loads — this
        is stored customer data."""
        config = AgentConfig.model_validate(
            {
                "system_prompt": "hello",
                "knowledge_bases": [{"knowledge_base_id": "kb-1", "top_k": 3}],
            }
        )

        assert config.system_prompt == "hello"
        assert config.knowledge_bases == []

    def test_absent_means_none(self) -> None:
        assert AgentConfig().knowledge_bases == []


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
