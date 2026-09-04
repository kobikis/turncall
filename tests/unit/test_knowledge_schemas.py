"""Tests for knowledge base API schema validation."""

import uuid

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.knowledge import (
    CreateKnowledgeBaseRequest,
    LinkKnowledgeBaseRequest,
    SearchRequest,
)


@pytest.mark.unit
class TestCreateKnowledgeBaseRequest:
    def test_valid_defaults(self) -> None:
        req = CreateKnowledgeBaseRequest(name="my-kb")
        assert req.name == "my-kb"
        assert req.embedding_model == "text-embedding-3-small"
        assert req.chunk_size == 512
        assert req.chunk_overlap == 64

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CreateKnowledgeBaseRequest(name="")

    def test_name_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CreateKnowledgeBaseRequest(name="x" * 256)

    def test_chunk_overlap_must_be_less_than_size(self) -> None:
        with pytest.raises(
            ValidationError, match="chunk_overlap must be less than chunk_size"
        ):
            CreateKnowledgeBaseRequest(name="kb", chunk_size=100, chunk_overlap=100)

    def test_chunk_overlap_greater_than_size_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="chunk_overlap must be less than chunk_size"
        ):
            CreateKnowledgeBaseRequest(name="kb", chunk_size=100, chunk_overlap=200)

    def test_custom_chunk_settings(self) -> None:
        req = CreateKnowledgeBaseRequest(name="kb", chunk_size=1024, chunk_overlap=128)
        assert req.chunk_size == 1024
        assert req.chunk_overlap == 128

    def test_chunk_size_bounds(self) -> None:
        with pytest.raises(ValidationError):
            CreateKnowledgeBaseRequest(name="kb", chunk_size=10)  # below min 64
        with pytest.raises(ValidationError):
            CreateKnowledgeBaseRequest(name="kb", chunk_size=5000)  # above max 4096


@pytest.mark.unit
class TestLinkKnowledgeBaseRequest:
    def test_valid_auto_mode(self) -> None:
        req = LinkKnowledgeBaseRequest(knowledge_base_id=uuid.uuid4(), mode="auto")
        assert req.mode == "auto"
        assert req.top_k == 5
        assert req.similarity_threshold == 0.3

    def test_valid_prompt_mode(self) -> None:
        req = LinkKnowledgeBaseRequest(knowledge_base_id=uuid.uuid4(), mode="prompt")
        assert req.mode == "prompt"

    def test_tool_mode_requires_description(self) -> None:
        with pytest.raises(
            ValidationError, match="tool_description is required when mode is 'tool'"
        ):
            LinkKnowledgeBaseRequest(knowledge_base_id=uuid.uuid4(), mode="tool")

    def test_tool_mode_with_description_valid(self) -> None:
        req = LinkKnowledgeBaseRequest(
            knowledge_base_id=uuid.uuid4(),
            mode="tool",
            tool_description="Search the docs for answers",
        )
        assert req.tool_description == "Search the docs for answers"

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LinkKnowledgeBaseRequest(knowledge_base_id=uuid.uuid4(), mode="invalid")

    def test_top_k_bounds(self) -> None:
        with pytest.raises(ValidationError):
            LinkKnowledgeBaseRequest(knowledge_base_id=uuid.uuid4(), top_k=0)
        with pytest.raises(ValidationError):
            LinkKnowledgeBaseRequest(knowledge_base_id=uuid.uuid4(), top_k=51)

    def test_similarity_threshold_bounds(self) -> None:
        LinkKnowledgeBaseRequest(
            knowledge_base_id=uuid.uuid4(), similarity_threshold=0.0
        )
        LinkKnowledgeBaseRequest(
            knowledge_base_id=uuid.uuid4(), similarity_threshold=1.0
        )
        with pytest.raises(ValidationError):
            LinkKnowledgeBaseRequest(
                knowledge_base_id=uuid.uuid4(), similarity_threshold=1.1
            )


@pytest.mark.unit
class TestSearchRequest:
    def test_valid_defaults(self) -> None:
        req = SearchRequest(query="what is the price?")
        assert req.top_k == 5
        assert req.similarity_threshold == 0.3

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="")

    def test_query_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="x" * 4001)

    def test_custom_params(self) -> None:
        req = SearchRequest(query="test", top_k=10, similarity_threshold=0.5)
        assert req.top_k == 10
        assert req.similarity_threshold == 0.5
