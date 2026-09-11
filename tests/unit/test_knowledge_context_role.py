"""Retrieved chunks are injected as `developer`, not `system`.

A second `"system"` message mid-context is not the agent's system prompt —
it is per-turn guidance. Pipecat made the same move for Mem0's memories in
1.9, and setting the system prompt through context messages stops working
in 2.0 entirely.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from turncall.orchestrator.knowledge_processor import KnowledgeRetrievalProcessor


class _SessionFactory:
    """`async with self._session_factory() as session` — an AsyncMock is not
    an async context manager, so the processor's except-clause would swallow
    the failure and the test would pass for the wrong reason."""

    def __call__(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _processor() -> KnowledgeRetrievalProcessor:
    return KnowledgeRetrievalProcessor(
        knowledge_base_ids=["kb-1"],
        session_factory=_SessionFactory(),
        openai_api_key="sk-test",
    )


@pytest.mark.unit
class TestInjectedRole:
    async def test_injected_message_uses_developer_role(self) -> None:
        proc = _processor()
        proc._last_user_text = "what are your hours?"
        frame = SimpleNamespace(messages=[{"role": "user", "content": "what are your hours?"}])

        result = SimpleNamespace(chunks=[SimpleNamespace(text="Open 9-5")])
        with (
            patch(
                "turncall.orchestrator.knowledge_processor.retrieve",
                AsyncMock(return_value=result),
            ),
            patch(
                "turncall.orchestrator.knowledge_processor.format_retrieved_context",
                return_value="Open 9-5",
            ),
        ):
            await proc._inject_context(frame)  # type: ignore[arg-type]

        assert frame.messages[0] == {"role": "developer", "content": "Open 9-5"}
        assert not any(m["role"] == "system" for m in frame.messages)

    async def test_nothing_injected_when_no_chunks(self) -> None:
        proc = _processor()
        proc._last_user_text = "hi"
        frame = SimpleNamespace(messages=[{"role": "user", "content": "hi"}])

        with patch(
            "turncall.orchestrator.knowledge_processor.retrieve",
            AsyncMock(return_value=SimpleNamespace(chunks=[])),
        ):
            await proc._inject_context(frame)  # type: ignore[arg-type]

        assert frame.messages == [{"role": "user", "content": "hi"}]
