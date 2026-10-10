"""`auto` knowledge retrieval on a voice call: the frame a call really sends.

The retrieval step sits between pipecat's user aggregator and the LLM. It
waited for a TranscriptionFrame and an LLMMessagesUpdateFrame — but pipecat
1.12's aggregator consumes transcriptions and hands each turn on as an
LLMContextFrame, so the step never fired: no voice call in `auto` mode ever
retrieved a passage (seen on a live call, 2026-10-10). These drive it with
the LLMContextFrame, and pin that the aggregator still sends one.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test

from turncall.orchestrator import knowledge_processor as kp


class _Sessions:
    def __call__(self):
        return self

    async def __aenter__(self):
        return AsyncMock()

    async def __aexit__(self, *_):
        return False


def _chunk(text: str):
    return SimpleNamespace(content=text, filename="menu.pdf", similarity=0.8)


def _processor() -> kp.KnowledgeRetrievalProcessor:
    return kp.KnowledgeRetrievalProcessor(
        knowledge_base_ids=[uuid.uuid4()], session_factory=_Sessions(), top_k=3
    )


def _kb_messages(context: LLMContext) -> list[dict]:
    return [
        m
        for m in context.get_messages()
        if "[Knowledge Base Context]" in str(m.get("content"))
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_turn_retrieves_and_the_passages_reach_the_llm():
    searched: list[str] = []

    async def retrieve(_session, **kw):
        searched.append(kw["query"])
        return SimpleNamespace(chunks=[_chunk("Salmon roll, 8 pcs")])

    context = LLMContext(messages=[{"role": "user", "content": "Do you have sushi?"}])
    with patch.object(kp, "retrieve", retrieve):
        down, _ = await run_test(
            _processor(), frames_to_send=[LLMContextFrame(context=context)]
        )

    assert [type(f).__name__ for f in down] == ["LLMContextFrame"]
    assert searched and "Do you have sushi?" in searched[0]
    messages = context.get_messages()
    # The passages sit right before the caller's turn they answer.
    assert "Salmon roll, 8 pcs" in str(messages[-2]["content"])
    assert messages[-1] == {"role": "user", "content": "Do you have sushi?"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_each_turn_replaces_the_last_turns_passages():
    """For this turn only: kept, a call would carry every turn's passages in
    its context for good — growing the prompt this exists to keep small."""
    answers = iter([[_chunk("Salmon roll")], [_chunk("Miso soup")]])

    async def retrieve(_session, **_kw):
        return SimpleNamespace(chunks=next(answers))

    context = LLMContext(messages=[{"role": "user", "content": "Sushi?"}])
    processor = _processor()
    with patch.object(kp, "retrieve", retrieve):
        await run_test(processor, frames_to_send=[LLMContextFrame(context=context)])
        context.add_message({"role": "assistant", "content": "We have salmon rolls."})
        context.add_message({"role": "user", "content": "And soup?"})
        await run_test(processor, frames_to_send=[LLMContextFrame(context=context)])

    [kb] = _kb_messages(context)
    assert "Miso soup" in str(kb["content"]) and "Salmon roll" not in str(kb["content"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nothing_found_leaves_no_stale_passages():
    answers = iter([[_chunk("Salmon roll")], []])

    async def retrieve(_session, **_kw):
        return SimpleNamespace(chunks=next(answers))

    context = LLMContext(messages=[{"role": "user", "content": "Sushi?"}])
    processor = _processor()
    with patch.object(kp, "retrieve", retrieve):
        await run_test(processor, frames_to_send=[LLMContextFrame(context=context)])
        context.add_message({"role": "user", "content": "What time do you close?"})
        await run_test(processor, frames_to_send=[LLMContextFrame(context=context)])

    assert _kb_messages(context) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_context_not_ending_on_the_caller_retrieves_nothing():
    """A run the caller did not start (a tool result, an idle check-in) has
    no new question to search for."""
    retrieve = AsyncMock()
    context = LLMContext(
        messages=[
            {"role": "user", "content": "Sushi?"},
            {"role": "assistant", "content": "Let me check."},
        ]
    )
    with patch.object(kp, "retrieve", retrieve):
        await run_test(_processor(), frames_to_send=[LLMContextFrame(context=context)])

    retrieve.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_turn_in_content_parts_is_read_too():
    searched: list[str] = []

    async def retrieve(_session, **kw):
        searched.append(kw["query"])
        return SimpleNamespace(chunks=[])

    context = LLMContext(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "Any dim sum?"}]}
        ]
    )
    with patch.object(kp, "retrieve", retrieve):
        await run_test(_processor(), frames_to_send=[LLMContextFrame(context=context)])

    assert "Any dim sum?" in searched[0]


@pytest.mark.unit
def test_pipecats_user_aggregator_still_hands_the_turn_on_as_a_context_frame():
    """What the step above listens for. If pipecat changes it again, this
    fails naming why, rather than `auto` going silent again."""
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
    )

    aggregator = LLMUserAggregator(LLMContext())
    assert isinstance(aggregator._get_context_frame(), LLMContextFrame)
