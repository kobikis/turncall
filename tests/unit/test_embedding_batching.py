"""generate_embeddings batches large inputs (review: a single unbatched request
hard-fails ingest of a big document past OpenAI's input limits)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.services import document_ingestion


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batches_over_the_limit():
    calls = []

    async def fake_create(*, input, model):
        calls.append(len(input))
        # one vector per input item, in order
        return MagicMock(data=[MagicMock(embedding=[float(i)]) for i in range(len(input))])

    client = MagicMock()
    client.embeddings.create = AsyncMock(side_effect=fake_create)

    with patch.object(document_ingestion, "_openai_client", return_value=client):
        texts = [f"chunk {i}" for i in range(600)]
        out = await document_ingestion.generate_embeddings(texts, api_key="k")

    assert len(out) == 600
    assert calls == [256, 256, 88]  # batched at _EMBED_BATCH, order preserved


@pytest.mark.unit
@pytest.mark.asyncio
async def test_small_input_single_batch():
    client = MagicMock()
    client.embeddings.create = AsyncMock(
        return_value=MagicMock(data=[MagicMock(embedding=[1.0]), MagicMock(embedding=[2.0])])
    )
    with patch.object(document_ingestion, "_openai_client", return_value=client):
        out = await document_ingestion.generate_embeddings(["a", "b"], api_key="k")
    assert out == [[1.0], [2.0]]
    assert client.embeddings.create.await_count == 1
