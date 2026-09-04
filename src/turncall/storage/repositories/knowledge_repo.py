"""Repository for knowledge base, document, and chunk operations."""

from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import (
    AgentKnowledgeBaseRow,
    DocumentChunkRow,
    DocumentRow,
    KnowledgeBaseRow,
)

# --- Knowledge Bases ---


async def create_knowledge_base(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    description: str | None = None,
    embedding_model: str = "text-embedding-3-small",
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> KnowledgeBaseRow:
    row = KnowledgeBaseRow(
        project_id=project_id,
        name=name,
        description=description,
        embedding_model=embedding_model,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    session.add(row)
    await session.flush()
    return row


async def get_knowledge_base_by_id(
    session: AsyncSession,
    kb_id: UUID,
    *,
    project_id: UUID | None = None,
) -> KnowledgeBaseRow | None:
    query = select(KnowledgeBaseRow).where(KnowledgeBaseRow.id == kb_id)
    if project_id is not None:
        query = query.where(KnowledgeBaseRow.project_id == project_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def list_knowledge_bases(
    session: AsyncSession,
    project_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[KnowledgeBaseRow]:
    query = (
        select(KnowledgeBaseRow)
        .where(KnowledgeBaseRow.project_id == project_id)
        .order_by(KnowledgeBaseRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def count_knowledge_bases(
    session: AsyncSession,
    project_id: UUID,
) -> int:
    query = select(func.count(KnowledgeBaseRow.id)).where(
        KnowledgeBaseRow.project_id == project_id
    )
    result = await session.execute(query)
    return result.scalar_one()


async def update_knowledge_base(
    session: AsyncSession,
    kb_id: UUID,
    *,
    name: str | None = None,
    description: str | None = ...,
    embedding_model: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> KnowledgeBaseRow | None:
    values: dict = {}
    if name is not None:
        values["name"] = name
    if description is not ...:
        values["description"] = description
    if embedding_model is not None:
        values["embedding_model"] = embedding_model
    if chunk_size is not None:
        values["chunk_size"] = chunk_size
    if chunk_overlap is not None:
        values["chunk_overlap"] = chunk_overlap
    if not values:
        return await get_knowledge_base_by_id(session, kb_id)
    result = await session.execute(
        update(KnowledgeBaseRow)
        .where(KnowledgeBaseRow.id == kb_id)
        .values(**values)
        .returning(KnowledgeBaseRow)
    )
    return result.scalar_one_or_none()


async def delete_knowledge_base(
    session: AsyncSession,
    kb_id: UUID,
) -> bool:
    result = await session.execute(
        delete(KnowledgeBaseRow).where(KnowledgeBaseRow.id == kb_id)
    )
    return result.rowcount > 0


# --- Documents ---


async def create_document(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    filename: str,
    content_type: str,
    storage_key: str,
    raw_text: str | None = None,
    char_count: int = 0,
    chunk_count: int = 0,
    status: str = "processing",
) -> DocumentRow:
    row = DocumentRow(
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        content_type=content_type,
        storage_key=storage_key,
        raw_text=raw_text,
        char_count=char_count,
        chunk_count=chunk_count,
        status=status,
    )
    session.add(row)
    await session.flush()
    return row


async def get_document_by_id(
    session: AsyncSession,
    doc_id: UUID,
    *,
    knowledge_base_id: UUID | None = None,
) -> DocumentRow | None:
    query = select(DocumentRow).where(DocumentRow.id == doc_id)
    if knowledge_base_id is not None:
        query = query.where(DocumentRow.knowledge_base_id == knowledge_base_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def list_documents(
    session: AsyncSession,
    knowledge_base_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[DocumentRow]:
    query = (
        select(DocumentRow)
        .where(DocumentRow.knowledge_base_id == knowledge_base_id)
        .order_by(DocumentRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def count_documents(
    session: AsyncSession,
    knowledge_base_id: UUID,
) -> int:
    query = select(func.count(DocumentRow.id)).where(
        DocumentRow.knowledge_base_id == knowledge_base_id
    )
    result = await session.execute(query)
    return result.scalar_one()


async def update_document_status(
    session: AsyncSession,
    doc_id: UUID,
    *,
    status: str,
    char_count: int | None = None,
    chunk_count: int | None = None,
    raw_text: str | None = None,
    error_message: str | None = None,
) -> DocumentRow | None:
    values: dict = {"status": status}
    if char_count is not None:
        values["char_count"] = char_count
    if chunk_count is not None:
        values["chunk_count"] = chunk_count
    if raw_text is not None:
        values["raw_text"] = raw_text
    if error_message is not None:
        values["error_message"] = error_message
    result = await session.execute(
        update(DocumentRow)
        .where(DocumentRow.id == doc_id)
        .values(**values)
        .returning(DocumentRow)
    )
    return result.scalar_one_or_none()


async def delete_document(
    session: AsyncSession,
    doc_id: UUID,
) -> bool:
    result = await session.execute(delete(DocumentRow).where(DocumentRow.id == doc_id))
    return result.rowcount > 0


# --- Document Chunks ---


async def create_chunks_batch(
    session: AsyncSession,
    chunks: list[dict],
) -> int:
    """Bulk insert chunks. Each dict must have: document_id, knowledge_base_id,
    chunk_index, content, token_count, embedding (list[float] or None)."""
    if not chunks:
        return 0
    for chunk in chunks:
        row = DocumentChunkRow(
            document_id=chunk["document_id"],
            knowledge_base_id=chunk["knowledge_base_id"],
            chunk_index=chunk["chunk_index"],
            content=chunk["content"],
            token_count=chunk["token_count"],
            # Set on insert (embedding is a mapped Vector column) — avoids a
            # re-SELECT of every chunk followed by a per-chunk UPDATE.
            embedding=chunk.get("embedding"),
        )
        session.add(row)
    await session.flush()
    return len(chunks)


async def set_chunk_embedding(
    session: AsyncSession,
    chunk_id: UUID,
    embedding: list[float],
) -> None:
    """Set embedding on a chunk using raw SQL for pgvector."""
    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
    await session.execute(
        update(DocumentChunkRow)
        .where(DocumentChunkRow.id == chunk_id)
        .values(embedding=func.cast(embedding_str, func.text("vector")))
    )


async def get_chunks_by_document(
    session: AsyncSession,
    document_id: UUID,
) -> list[DocumentChunkRow]:
    query = (
        select(DocumentChunkRow)
        .where(DocumentChunkRow.document_id == document_id)
        .order_by(DocumentChunkRow.chunk_index)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def delete_chunks_by_document(
    session: AsyncSession,
    document_id: UUID,
) -> int:
    result = await session.execute(
        delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document_id)
    )
    return result.rowcount


# --- Agent ↔ Knowledge Base Links ---


async def link_agent_knowledge_base(
    session: AsyncSession,
    *,
    agent_id: UUID,
    knowledge_base_id: UUID,
    mode: str = "auto",
    priority: int = 0,
    top_k: int = 5,
    similarity_threshold: float = 0.3,
    tool_description: str | None = None,
) -> AgentKnowledgeBaseRow:
    row = AgentKnowledgeBaseRow(
        agent_id=agent_id,
        knowledge_base_id=knowledge_base_id,
        mode=mode,
        priority=priority,
        top_k=top_k,
        similarity_threshold=similarity_threshold,
        tool_description=tool_description,
    )
    session.add(row)
    await session.flush()
    return row


async def unlink_agent_knowledge_base(
    session: AsyncSession,
    *,
    agent_id: UUID,
    knowledge_base_id: UUID,
) -> bool:
    result = await session.execute(
        delete(AgentKnowledgeBaseRow).where(
            AgentKnowledgeBaseRow.agent_id == agent_id,
            AgentKnowledgeBaseRow.knowledge_base_id == knowledge_base_id,
        )
    )
    return result.rowcount > 0


async def get_agent_knowledge_bases(
    session: AsyncSession,
    agent_id: UUID,
) -> list[AgentKnowledgeBaseRow]:
    query = (
        select(AgentKnowledgeBaseRow)
        .where(AgentKnowledgeBaseRow.agent_id == agent_id)
        .order_by(AgentKnowledgeBaseRow.priority)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_agent_kb_attachments(
    session: AsyncSession,
    agent_id: UUID,
) -> list[dict]:
    """Agent's KB attachments as the plain-dict shape create_pipeline expects
    (knowledge_base_id, mode, top_k, similarity_threshold). Empty list when the
    agent has no knowledge bases — the pipeline then skips KB retrieval."""
    rows = await get_agent_knowledge_bases(session, agent_id)
    return [
        {
            "knowledge_base_id": r.knowledge_base_id,
            "mode": r.mode,
            "priority": r.priority,
            "top_k": r.top_k,
            "similarity_threshold": r.similarity_threshold,
            "tool_description": r.tool_description,
        }
        for r in rows
    ]


async def count_agents_using_kb(
    session: AsyncSession,
    knowledge_base_id: UUID,
) -> int:
    query = select(func.count(AgentKnowledgeBaseRow.agent_id)).where(
        AgentKnowledgeBaseRow.knowledge_base_id == knowledge_base_id
    )
    result = await session.execute(query)
    return result.scalar_one()


async def get_all_documents_text(
    session: AsyncSession,
    knowledge_base_id: UUID,
) -> list[DocumentRow]:
    """Get all ready documents for a KB (for prompt mode — full text injection).
    Undefers raw_text (deferred by default) since prompt mode needs it."""
    from sqlalchemy.orm import undefer

    query = (
        select(DocumentRow)
        .where(
            DocumentRow.knowledge_base_id == knowledge_base_id,
            DocumentRow.status == "ready",
        )
        .options(undefer(DocumentRow.raw_text))
        .order_by(DocumentRow.created_at)
    )
    result = await session.execute(query)
    return list(result.scalars().all())
