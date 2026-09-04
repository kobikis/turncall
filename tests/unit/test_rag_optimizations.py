"""ADR-0012 RAG pieces: query windowing + enrichment fallback."""

import pytest

from turncall.orchestrator.knowledge_processor import build_retrieval_query
from turncall.services.document_ingestion import enrich_chunks


@pytest.mark.unit
class TestBuildRetrievalQuery:
    def test_windows_previous_turn_and_reply(self) -> None:
        messages = [
            {"role": "system", "content": "You are a receptionist."},
            {"role": "user", "content": "what is my flight date?"},
            {"role": "assistant", "content": "Your flight is on Monday 13 July 2026."},
            {"role": "user", "content": "and what time?"},
        ]
        q = build_retrieval_query(messages, "and what time?")
        assert "flight date" in q          # previous user turn
        assert "13 July 2026" in q         # entities from the agent's reply
        assert q.strip().endswith("and what time?")  # recency last

    def test_single_turn_is_just_the_utterance(self) -> None:
        q = build_retrieval_query([{"role": "user", "content": "hi"}], "hi")
        assert q == "hi"

    def test_ignores_non_string_content_and_caps_length(self) -> None:
        messages = [
            {"role": "assistant", "content": [{"type": "image"}]},
            {"role": "assistant", "content": "x" * 5000},
        ]
        q = build_retrieval_query(messages, "short question", max_chars=100)
        assert len(q) <= 100
        assert q.endswith("short question")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_chunks_falls_back_without_api_key() -> None:
    chunks = ["first chunk", "second chunk"]
    out = await enrich_chunks(
        chunks, full_text="doc", filename="booking.pdf", api_key=""
    )
    assert out == ["[booking.pdf]\nfirst chunk", "[booking.pdf]\nsecond chunk"]
