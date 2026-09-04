"""Tests for retrieval service: context formatting."""

import uuid

import pytest

from turncall.services.retrieval import (
    RetrievalResult,
    RetrievedChunk,
    format_retrieved_context,
)


@pytest.mark.unit
class TestFormatRetrievedContext:
    def _make_chunk(
        self, content: str = "Some content", similarity: float = 0.9, index: int = 0
    ) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            content=content,
            similarity=similarity,
            chunk_index=index,
            token_count=10,
        )

    def test_empty_chunks_returns_empty(self) -> None:
        result = RetrievalResult(chunks=[], query="test", knowledge_base_ids=[])
        assert format_retrieved_context(result) == ""

    def test_single_chunk_formatted(self) -> None:
        chunk = self._make_chunk("Product costs $49.99", similarity=0.85)
        result = RetrievalResult(
            chunks=[chunk], query="price", knowledge_base_ids=[uuid.uuid4()]
        )
        text = format_retrieved_context(result)
        assert "[Knowledge Base Context]" in text
        assert "Product costs $49.99" in text
        assert "[End Knowledge Base Context]" in text

    def test_multiple_chunks_numbered(self) -> None:
        chunks = [
            self._make_chunk("First chunk", similarity=0.95, index=0),
            self._make_chunk("Second chunk", similarity=0.80, index=1),
            self._make_chunk("Third chunk", similarity=0.75, index=2),
        ]
        result = RetrievalResult(
            chunks=chunks, query="test", knowledge_base_ids=[uuid.uuid4()]
        )
        text = format_retrieved_context(result)
        assert "Source 1" in text
        assert "Source 2" in text
        assert "Source 3" in text
        assert "First chunk" in text
        assert "Third chunk" in text

    def test_filename_shown_when_present(self) -> None:
        """Sources cite the document, not a score — a lexical-only hit has no
        cosine similarity, and 'relevance: 0.00' would mislead the LLM."""
        chunk = RetrievedChunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            content="Departure 08:45",
            similarity=0.0,
            chunk_index=0,
            token_count=5,
            filename="booking.pdf",
        )
        result = RetrievalResult(
            chunks=[chunk], query="q", knowledge_base_ids=[uuid.uuid4()]
        )
        text = format_retrieved_context(result)
        assert "booking.pdf" in text
        assert "0.0" not in text


@pytest.mark.unit
class TestRetrievedChunkImmutability:
    def test_chunk_is_frozen(self) -> None:
        chunk = RetrievedChunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            content="test",
            similarity=0.9,
            chunk_index=0,
            token_count=5,
        )
        with pytest.raises(Exception):
            chunk.content = "mutated"  # type: ignore[misc]

    def test_result_is_frozen(self) -> None:
        result = RetrievalResult(chunks=[], query="test", knowledge_base_ids=[])
        with pytest.raises(Exception):
            result.query = "mutated"  # type: ignore[misc]
