"""Knowledge base retrieval processor for the Pipecat pipeline.

Supports three modes:
- auto: Retrieves on every user turn, injects context before LLM
- tool: Registers a query_knowledge function the LLM can call
- prompt: Full text injected at pipeline creation (handled externally)
"""

from typing import Any
from uuid import UUID

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMMessagesUpdateFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.services.retrieval import format_retrieved_context, retrieve


async def load_agent_kb_attachments(
    session_factory: async_sessionmaker[AsyncSession],
    agent_id: UUID,
) -> list[dict]:
    """Load an agent's KB attachments for create_pipeline. Best-effort: a load
    failure logs and returns [] so a call still connects (without KB) rather
    than failing outright."""
    from turncall.storage.repositories import knowledge_repo

    try:
        async with session_factory() as session:
            return await knowledge_repo.get_agent_kb_attachments(session, agent_id)
    except Exception:
        logger.exception("kb_attachments_load_failed for agent {aid}", aid=str(agent_id))
        return []


class KnowledgeRetrievalProcessor(FrameProcessor):
    """Intercepts user transcription frames and injects retrieved KB context.

    Sits between user_aggregator and LLM in the pipeline:
      user_agg → KnowledgeRetrievalProcessor → LLM
    """

    def __init__(
        self,
        *,
        knowledge_base_ids: list[UUID],
        session_factory: async_sessionmaker[AsyncSession],
        openai_api_key: str = "",
        embedding_model: str = "text-embedding-3-small",
        top_k: int = 5,
        similarity_threshold: float = 0.3,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._kb_ids = knowledge_base_ids
        self._session_factory = session_factory
        self._openai_api_key = openai_api_key
        self._embedding_model = embedding_model
        self._top_k = top_k
        self._similarity_threshold = similarity_threshold
        self._last_user_text: str = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Process frames flowing through the pipeline."""
        # Required by Pipecat 1.4: the base handles Start/interruption/cancel
        # frames and sets the started flag — without it push_frame drops
        # everything and the pipeline stalls at this processor.
        await super().process_frame(frame, direction)

        # Capture the latest user transcription text
        if isinstance(frame, TranscriptionFrame):
            if frame.text and frame.text.strip():
                self._last_user_text = frame.text.strip()

        # When LLM messages are being sent, inject retrieved context
        if isinstance(frame, LLMMessagesUpdateFrame) and self._last_user_text:
            await self._inject_context(frame)
            self._last_user_text = ""

        await self.push_frame(frame, direction)

    async def _inject_context(self, frame: LLMMessagesUpdateFrame) -> None:
        """Retrieve relevant chunks and inject as a system message."""
        try:
            query = build_retrieval_query(frame.messages, self._last_user_text)
            async with self._session_factory() as session:
                result = await retrieve(
                    session,
                    query=query,
                    knowledge_base_ids=self._kb_ids,
                    top_k=self._top_k,
                    similarity_threshold=self._similarity_threshold,
                    openai_api_key=self._openai_api_key,
                    embedding_model=self._embedding_model,
                )

            if result.chunks:
                context_text = format_retrieved_context(result)
                # Prepend retrieved context as a system message
                context_msg = {"role": "system", "content": context_text}
                frame.messages.insert(0, context_msg)
                logger.debug(
                    "KB auto-retrieval: injected {n} chunks for: {q:.60}",
                    n=len(result.chunks),
                    q=self._last_user_text,
                )
        except Exception:
            logger.exception("Knowledge retrieval failed, continuing without context")


def build_retrieval_query(
    messages: list[dict], current: str, max_chars: int = 600
) -> str:
    """Window the retrieval query: previous user turn + last agent reply +
    current utterance (ADR-0012). Voice follow-ups are anaphoric ("and what
    time?") — a lone fragment gives both search legs nothing; the window
    carries the entities the conversation is actually about. Recency last,
    capped so an essay-length reply can't drown the question."""

    def _text(m: dict) -> str:
        content = m.get("content")
        return content.strip() if isinstance(content, str) else ""

    current = current.strip()
    prev_user = next(
        (
            _text(m)
            for m in reversed(messages)
            if m.get("role") == "user" and _text(m) and _text(m) != current
        ),
        "",
    )
    last_assistant = next(
        (_text(m) for m in reversed(messages) if m.get("role") == "assistant" and _text(m)),
        "",
    )
    query = "\n".join(p for p in (prev_user, last_assistant, current) if p)
    return query[-max_chars:]


def create_knowledge_tool_handler(
    *,
    knowledge_base_ids: list[UUID],
    session_factory: async_sessionmaker[AsyncSession],
    openai_api_key: str = "",
    embedding_model: str = "text-embedding-3-small",
    top_k: int = 5,
    similarity_threshold: float = 0.3,
) -> Any:
    """Create a tool handler function for query_knowledge (tool mode).

    Returns an async function compatible with Pipecat's FunctionCallParams.
    """

    async def handler(params: Any) -> None:
        """Handle query_knowledge tool call from LLM."""
        query = params.arguments.get("query", "")
        if not query:
            await params.result_callback({"error": "query parameter is required"})
            return

        try:
            async with session_factory() as session:
                result = await retrieve(
                    session,
                    query=query,
                    knowledge_base_ids=knowledge_base_ids,
                    top_k=top_k,
                    similarity_threshold=similarity_threshold,
                    openai_api_key=openai_api_key,
                    embedding_model=embedding_model,
                )

            if result.chunks:
                context = format_retrieved_context(result)
                await params.result_callback(
                    {"results": context, "count": len(result.chunks)}
                )
            else:
                await params.result_callback(
                    {"results": "No relevant information found.", "count": 0}
                )

        except Exception as exc:
            logger.exception("Knowledge tool query failed")
            await params.result_callback({"error": str(exc)})

    return handler


# Tool definition for registering query_knowledge on the LLM
KNOWLEDGE_TOOL_SCHEMA = {
    "name": "query_knowledge",
    "description": (
        "Search the knowledge base for relevant information "
        "to answer the user's question."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to find relevant information",
            },
        },
        "required": ["query"],
    },
}
