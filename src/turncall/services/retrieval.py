"""Knowledge base retrieval: embed query, search pgvector, format results."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class RetrievedChunk:
    """A single retrieved chunk with similarity score."""

    chunk_id: UUID
    document_id: UUID
    content: str
    similarity: float
    chunk_index: int
    token_count: int
    filename: str = ""


@dataclass(frozen=True)
class RetrievalResult:
    """Result of a knowledge base retrieval query."""

    chunks: list[RetrievedChunk]
    query: str
    knowledge_base_ids: list[UUID]


async def retrieve(
    session: AsyncSession,
    *,
    query: str,
    knowledge_base_ids: list[UUID],
    top_k: int = 5,
    similarity_threshold: float = 0.3,
    openai_api_key: str = "",
    embedding_model: str = "text-embedding-3-small",
) -> RetrievalResult:
    """Hybrid retrieval: vector KNN + full-text search, fused by rank (RRF).

    The vector leg carries semantic similarity (threshold applies to it); the
    lexical leg catches exact-term matches ("flight date", codes) that
    embeddings under-score on record-like documents. Reciprocal Rank Fusion
    combines them by rank, so there are no score weights to tune. If query
    embedding fails (e.g. no OpenAI key), retrieval degrades to lexical-only
    instead of failing.
    """
    if not knowledge_base_ids or not query.strip():
        return RetrievalResult(
            chunks=[], query=query, knowledge_base_ids=knowledge_base_ids
        )

    # 1. Generate query embedding — best-effort; lexical leg still works without.
    embedding_str = None
    try:
        from turncall.services.document_ingestion import generate_embeddings

        embeddings = await generate_embeddings(
            [query], model=embedding_model, api_key=openai_api_key
        )
        embedding_str = "[" + ",".join(str(v) for v in embeddings[0]) + "]"
    except Exception:
        logger.warning("query_embedding_failed — falling back to lexical-only search")

    # 2. One fused query. CAST() instead of :: (SQLAlchemy :param conflict).
    # Each leg over-fetches (top_k * 4) so fusion has candidates to work with.
    sql = text(
        """
        WITH vec AS (
            SELECT id,
                   1 - (embedding <=> CAST(:query_embedding AS vector)) AS similarity,
                   ROW_NUMBER() OVER (
                       ORDER BY embedding <=> CAST(:query_embedding AS vector)
                   ) AS rnk
            FROM document_chunks
            WHERE :query_embedding IS NOT NULL
              AND knowledge_base_id = ANY(CAST(:kb_ids AS uuid[]))
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:query_embedding AS vector)) >= :threshold
            ORDER BY embedding <=> CAST(:query_embedding AS vector)
            LIMIT :pool
        ),
        lex AS (
            SELECT c.id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS rnk
            FROM document_chunks c,
                 websearch_to_tsquery('english', :query_text) q
            WHERE c.knowledge_base_id = ANY(CAST(:kb_ids AS uuid[]))
              AND c.tsv @@ q
            LIMIT :pool
        ),
        fused AS (
            SELECT id, SUM(1.0 / (60 + rnk)) AS rrf
            FROM (SELECT id, rnk FROM vec UNION ALL SELECT id, rnk FROM lex) u
            GROUP BY id
        )
        SELECT c.id, c.document_id, c.content, c.chunk_index, c.token_count,
               COALESCE(v.similarity, 0.0) AS similarity,
               d.filename
        FROM fused f
        JOIN document_chunks c ON c.id = f.id
        LEFT JOIN vec v ON v.id = f.id
        LEFT JOIN documents d ON d.id = c.document_id
        ORDER BY f.rrf DESC
        LIMIT :top_k
    """
    )

    result = await session.execute(
        sql,
        {
            "query_embedding": embedding_str,
            "query_text": query,
            "kb_ids": [str(kb_id) for kb_id in knowledge_base_ids],
            "threshold": similarity_threshold,
            "pool": max(top_k * 4, 20),
            "top_k": top_k,
        },
    )
    rows = result.fetchall()

    chunks = [
        RetrievedChunk(
            chunk_id=row.id,
            document_id=row.document_id,
            content=row.content,
            similarity=row.similarity,
            chunk_index=row.chunk_index,
            token_count=row.token_count,
            filename=row.filename or "",
        )
        for row in rows
    ]

    logger.debug(
        "Hybrid retrieval: {n} chunks (threshold={t}) for query: {q:.80}",
        n=len(chunks),
        t=similarity_threshold,
        q=query,
    )

    return RetrievalResult(
        chunks=chunks,
        query=query,
        knowledge_base_ids=knowledge_base_ids,
    )


def format_retrieved_context(result: RetrievalResult) -> str:
    """Format retrieved chunks into a context string for the LLM prompt.

    Returns a structured string that can be prepended to the system prompt.
    """
    if not result.chunks:
        return ""

    parts = ["[Knowledge Base Context]"]
    for i, chunk in enumerate(result.chunks, 1):
        source = f" — {chunk.filename}" if chunk.filename else ""
        parts.append(f"--- Source {i}{source} ---")
        parts.append(chunk.content)

    parts.append("[End Knowledge Base Context]")
    return "\n\n".join(parts)


async def get_full_text_context(
    session: AsyncSession,
    knowledge_base_ids: list[UUID],
) -> str:
    """Get full document text for prompt mode (small docs injected entirely).

    Returns concatenated text from all ready documents in the given KBs.
    """
    from turncall.storage.repositories import knowledge_repo

    parts: list[str] = []
    for kb_id in knowledge_base_ids:
        docs = await knowledge_repo.get_all_documents_text(session, kb_id)
        for doc in docs:
            if doc.raw_text:
                parts.append(f"--- {doc.filename} ---")
                parts.append(doc.raw_text)

    if not parts:
        return ""

    return (
        "[Knowledge Base Context]\n\n"
        + "\n\n".join(parts)
        + "\n\n[End Knowledge Base Context]"
    )


# Told to the model when a retrieval-based KB (auto/tool) is attached, so it
# doesn't deny having a knowledge base when a question happens not to retrieve
# (e.g. a meta-request like "use the attached file").
KNOWLEDGE_AWARENESS_HINT = (
    "You have a knowledge base of reference documents available. When the caller "
    "asks about something it may cover, answer from it — and never tell them you "
    "don't have access to a knowledge base or attached files, because you do."
)


async def build_knowledge_preamble(
    session_factory: Any, attachments: list[dict[str, Any]]
) -> str:
    """System-prompt additions for a KB-attached agent: the full text of any
    prompt-mode KBs (always present, no retrieval needed) plus an awareness hint
    when auto/tool KBs are attached. Empty string when nothing is attached."""
    if not attachments:
        return ""
    parts: list[str] = []
    prompt_ids = [
        UUID(str(a["knowledge_base_id"]))
        for a in attachments
        if a.get("mode") == "prompt"
    ]
    if prompt_ids:
        async with session_factory() as session:
            full = await get_full_text_context(session, prompt_ids)
        if full:
            parts.append(full)
    if any(a.get("mode") in ("auto", "tool") for a in attachments):
        parts.append(KNOWLEDGE_AWARENESS_HINT)
    return "\n\n".join(parts)
