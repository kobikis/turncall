"""Knowledge base retrieval processor for the Pipecat pipeline.

Supports three modes:
- auto: Retrieves on every user turn, injects context before LLM
- tool: Registers a query_knowledge function the LLM can call
- prompt: Full text injected at pipeline creation (handled externally)
"""

from collections.abc import Callable
from typing import Any, NamedTuple
from uuid import UUID

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.domain.models import KnowledgeBaseAttachment, ToolDefinition
from turncall.orchestrator.turn_message import replace_turn_message, text_of
from turncall.services.retrieval import (
    KNOWLEDGE_TOOL_SCHEMA,
    format_retrieved_context,
    query_knowledge,
    retrieve,
)


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
        logger.exception(
            "kb_attachments_load_failed for agent {aid}", aid=str(agent_id)
        )
        return []


async def load_inline_kb_attachments(
    session_factory: async_sessionmaker[AsyncSession],
    project_id: UUID,
    attachments: list[KnowledgeBaseAttachment],
) -> list[dict[str, Any]]:
    """An inline agent's knowledge bases (#260), in the shape the link rows
    give. Each id resolves against the call's project, never the config's word
    for it: a KB from another project, or none at all, is skipped with a
    warning and the call still connects."""
    from turncall.storage.repositories import knowledge_repo

    usable = []
    seen = set()
    try:
        async with session_factory() as session:
            # ponytail: one lookup per KB — an agent names a handful.
            for att in sorted(attachments, key=lambda a: a.priority):
                # A link row is unique per KB; a list is not, and a repeat
                # would put a prompt-mode document in the instructions twice.
                if att.knowledge_base_id in seen:
                    continue
                seen.add(att.knowledge_base_id)
                kb = await knowledge_repo.get_knowledge_base_by_id(
                    session, att.knowledge_base_id, project_id=project_id
                )
                if kb is None:
                    logger.warning(
                        "knowledge_base_skipped: {kb} is not a knowledge base in "
                        "project {project}, call runs without it",
                        kb=str(att.knowledge_base_id),
                        project=str(project_id),
                    )
                    continue
                usable.append(att.model_dump())
    except Exception:
        logger.exception(
            "kb_attachments_load_failed for inline agent in project {pid}",
            pid=str(project_id),
        )
        return []
    return usable


# Every retrieved-context message starts with this (format_retrieved_context),
# which is how a turn finds the previous turn's passages to take out.
_PASSAGES_MARKER = "[Knowledge Base Context]"


class KnowledgeRetrievalProcessor(FrameProcessor):
    """Retrieves passages for each caller turn and puts them before it.

    Sits between user_aggregator and LLM in the pipeline:
      user_agg → KnowledgeRetrievalProcessor → LLM

    pipecat's user aggregator hands each turn on as an `LLMContextFrame` —
    it consumes the transcription itself — so that is what this acts on: the
    turn's text is the context's last message. It waited for a transcription
    and an `LLMMessagesUpdateFrame` instead, neither of which reaches it since
    pipecat 1.x's universal context, so `auto` never retrieved on a call.

    The passages are for one turn: the context is the call's history, so the
    previous turn's are taken out before this turn's go in, or every turn's
    would stay in the prompt for good.
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

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Process frames flowing through the pipeline."""
        # Required by Pipecat 1.4: the base handles Start/interruption/cancel
        # frames and sets the started flag — without it push_frame drops
        # everything and the pipeline stalls at this processor.
        await super().process_frame(frame, direction)

        if (
            isinstance(frame, LLMContextFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            await self._retrieve_for(frame.context)

        await self.push_frame(frame, direction)

    async def _retrieve_for(self, context: Any) -> None:
        """Replace the previous turn's passages with this turn's, if any."""
        await replace_turn_message(context, _PASSAGES_MARKER, self._passages)

    async def _passages(
        self, messages: list[Any], current: str
    ) -> dict[str, str] | None:
        """This turn's passages as a message, or None for none found or a
        failed search — the turn goes on without them either way."""
        try:
            query = build_retrieval_query(messages, current)
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
        except Exception:
            logger.exception("Knowledge retrieval failed, continuing without context")
            return None
        if not result.chunks:
            return None
        logger.debug(
            "KB auto-retrieval: {n} chunks for: {q:.60}",
            n=len(result.chunks),
            q=current,
        )
        # "developer", not "system": retrieved chunks are guidance for this
        # turn, not the agent's system prompt. Pipecat moved Mem0's memories
        # for the same reason in 1.9, and a second "system" message
        # mid-context is downgraded to "user" by non-OpenAI services anyway.
        return {"role": "developer", "content": format_retrieved_context(result)}


def build_retrieval_query(
    messages: list[dict], current: str, max_chars: int = 600
) -> str:
    """Window the retrieval query: previous user turn + last agent reply +
    current utterance (ADR-0012). Voice follow-ups are anaphoric ("and what
    time?") — a lone fragment gives both search legs nothing; the window
    carries the entities the conversation is actually about. Recency last,
    capped so an essay-length reply can't drown the question."""

    _text = text_of

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
        (
            _text(m)
            for m in reversed(messages)
            if m.get("role") == "assistant" and _text(m)
        ),
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
    return _search_handler(
        lambda: (knowledge_base_ids, top_k, similarity_threshold),
        session_factory=session_factory,
        openai_api_key=openai_api_key,
        embedding_model=embedding_model,
    )


def _search_handler(
    searched: Callable[[], tuple[list[UUID], int, float]],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    openai_api_key: str,
    embedding_model: str = "text-embedding-3-small",
) -> Any:
    """`query_knowledge`'s handler over `searched()` — the KB ids, `top_k` and
    threshold, read on each call."""

    async def handler(params: Any) -> None:
        """Handle query_knowledge tool call from LLM."""
        query = params.arguments.get("query", "")
        if not query:
            await params.result_callback({"error": "query parameter is required"})
            return

        knowledge_base_ids, top_k, similarity_threshold = searched()
        try:
            async with session_factory() as session:
                found = await query_knowledge(
                    session,
                    query=query,
                    knowledge_base_ids=knowledge_base_ids,
                    top_k=top_k,
                    similarity_threshold=similarity_threshold,
                    openai_api_key=openai_api_key,
                    embedding_model=embedding_model,
                )
            await params.result_callback(found)

        except Exception:
            # Why goes to the log, not the model: it can say it aloud.
            logger.exception("Knowledge tool query failed")
            await params.result_callback({"error": "knowledge search failed"})

    return handler


class KnowledgeTool(NamedTuple):
    """`query_knowledge` for an agent's tool-mode KBs: what is advertised, and
    the handler whoever calls tools registers under its name."""

    definition: ToolDefinition
    handler: Any


def knowledge_tool_definition(
    attachments: list[dict[str, Any]] | None,
) -> ToolDefinition | None:
    """What `query_knowledge` is advertised as for these attachments, None
    without a tool-mode KB. Settings come from the first of them."""
    tool_kbs = [a for a in attachments or [] if a.get("mode") == "tool"]
    if not tool_kbs:
        return None
    return ToolDefinition(
        name=KNOWLEDGE_TOOL_SCHEMA["name"],
        description=tool_kbs[0].get("tool_description")
        or KNOWLEDGE_TOOL_SCHEMA["description"],
        parameters_schema=KNOWLEDGE_TOOL_SCHEMA["parameters"],
    )


def knowledge_tool(
    attachments: list[dict[str, Any]] | None,
    session_factory: async_sessionmaker[AsyncSession],
    openai_api_key: str,
    call_context: Any | None = None,
) -> KnowledgeTool | None:
    """One tool over every tool-mode KB, None without any. Settings come from
    the first of them. With the call's context its calls are logged and
    recorded like every other tool's."""
    definition = knowledge_tool_definition(attachments)
    if definition is None:
        return None
    tool_kbs = [a for a in attachments or [] if a.get("mode") == "tool"]
    first = tool_kbs[0]
    handler = create_knowledge_tool_handler(
        knowledge_base_ids=[a["knowledge_base_id"] for a in tool_kbs],
        session_factory=session_factory,
        openai_api_key=openai_api_key,
        top_k=first.get("top_k", 5),
        similarity_threshold=first.get("similarity_threshold", 0.3),
    )
    if call_context is not None:
        from turncall.orchestrator.tool_bridge import recorded_tool

        handler = recorded_tool(definition.name, handler, call_context)
    logger.info("KB tool mode enabled for {n} KBs", n=len(tool_kbs))
    return KnowledgeTool(definition, handler)


def searching_knowledge_tool(
    searched: Callable[[], list[dict[str, Any]]],
    attachments: list[dict[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
    openai_api_key: str,
    call_context: Any,
) -> KnowledgeTool | None:
    """`query_knowledge` over whichever tool-mode attachments `searched()`
    names when the model calls it — the agent's own and its loaded skills'
    (#429), so a skill unloaded since is no longer searched. Advertised as
    `attachments` would be (None without a tool-mode one); settings come from
    the first searched, as for `knowledge_tool`. Recorded like it."""
    definition = knowledge_tool_definition(attachments)
    if definition is None:
        return None

    def settings() -> tuple[list[UUID], int, float]:
        attachments = searched()
        first = attachments[0] if attachments else {}
        return (
            [a["knowledge_base_id"] for a in attachments],
            first.get("top_k", 5),
            first.get("similarity_threshold", 0.3),
        )

    from turncall.orchestrator.tool_bridge import recorded_tool

    handler = _search_handler(
        settings, session_factory=session_factory, openai_api_key=openai_api_key
    )
    return KnowledgeTool(
        definition, recorded_tool(definition.name, handler, call_context)
    )
