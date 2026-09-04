"""Knowledge base API endpoints."""

from uuid import UUID

from fastapi import APIRouter, UploadFile

from turncall.adapters.storage import create_storage_adapter
from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ConflictError, ErrorCode, NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.knowledge import (
    AgentKnowledgeBaseResponse,
    CreateKnowledgeBaseRequest,
    DocumentResponse,
    KnowledgeBaseResponse,
    LinkKnowledgeBaseRequest,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    UpdateKnowledgeBaseRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.config.settings import get_settings
from turncall.services.document_ingestion import (
    create_pending_document,
    ingest_in_background,
)
from turncall.services.retrieval import retrieve
from turncall.storage.repositories import knowledge_repo

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/tab-separated-values",
    "text/yaml",
    "text/xml",
    "application/pdf",
    "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}


# --- Knowledge Bases ---


@router.post("", status_code=201)
async def create_knowledge_base(
    body: CreateKnowledgeBaseRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Create a new knowledge base."""
    row = await knowledge_repo.create_knowledge_base(
        session,
        project_id=auth.project_id,
        name=body.name,
        description=body.description,
        embedding_model=body.embedding_model,
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
    )
    await session.commit()
    return ok(KnowledgeBaseResponse.from_row(row))


@router.get("")
async def list_knowledge_bases(
    auth: Auth,
    session: DbSession,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """List knowledge bases for the authenticated project."""
    offset = (page - 1) * limit
    rows = await knowledge_repo.list_knowledge_bases(
        session, auth.project_id, limit=limit, offset=offset
    )
    total = await knowledge_repo.count_knowledge_bases(session, auth.project_id)
    return paginated(
        data=[KnowledgeBaseResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/{kb_id}")
async def get_knowledge_base(
    kb_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get a knowledge base by ID."""
    row = await knowledge_repo.get_knowledge_base_by_id(
        session, kb_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))
    return ok(KnowledgeBaseResponse.from_row(row))


@router.put("/{kb_id}")
async def update_knowledge_base(
    kb_id: UUID,
    body: UpdateKnowledgeBaseRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Update a knowledge base."""
    existing = await knowledge_repo.get_knowledge_base_by_id(
        session, kb_id, project_id=auth.project_id
    )
    if existing is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))
    row = await knowledge_repo.update_knowledge_base(
        session,
        kb_id,
        name=body.name,
        description=body.description,
    )
    await session.commit()
    return ok(KnowledgeBaseResponse.from_row(row))


@router.delete("/{kb_id}", status_code=204)
async def delete_knowledge_base(
    kb_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> None:
    """Delete a knowledge base. Fails if agents depend on it."""
    existing = await knowledge_repo.get_knowledge_base_by_id(
        session, kb_id, project_id=auth.project_id
    )
    if existing is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    agent_count = await knowledge_repo.count_agents_using_kb(session, kb_id)
    if agent_count > 0:
        raise ConflictError(
            f"Cannot delete knowledge base: {agent_count} agent(s) depend on it. "
            "Unlink all agents first.",
            details={"agent_count": agent_count},
        )

    await knowledge_repo.delete_knowledge_base(session, kb_id)
    await session.commit()


# --- Documents ---


@router.post("/{kb_id}/documents", status_code=202)
async def upload_document(
    kb_id: UUID,
    file: UploadFile,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Upload a document. Returns 202 with a 'processing' document immediately;
    extract/chunk/embed runs in the background (it can take minutes on a large
    file). Poll GET /{kb_id}/documents/{doc_id} for the ready/failed transition."""
    kb_row = await knowledge_repo.get_knowledge_base_by_id(
        session, kb_id, project_id=auth.project_id
    )
    if kb_row is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    content_type = file.content_type or "application/octet-stream"
    if content_type not in ALLOWED_CONTENT_TYPES:
        # 415 Unsupported Media Type — a bad file type is not a 409 conflict.
        raise ApiError(
            status_code=415,
            code=ErrorCode.VALIDATION_ERROR,
            message=(
                f"Unsupported file type: {content_type}. "
                f"Allowed: {', '.join(sorted(ALLOWED_CONTENT_TYPES))}"
            ),
        )

    # Reject oversized uploads by Content-Length before reading the body, so a
    # flood of huge uploads can't balloon RSS before being rejected. (The header
    # can be absent/wrong, so the post-read check below remains authoritative.)
    if file.size is not None and file.size > MAX_UPLOAD_SIZE:
        raise ConflictError(
            f"File too large: {file.size} bytes. Maximum: {MAX_UPLOAD_SIZE} bytes."
        )

    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise ConflictError(
            f"File too large: {len(data)} bytes. Maximum: {MAX_UPLOAD_SIZE} bytes."
        )

    settings = get_settings()
    storage = create_storage_adapter(
        settings.storage.backend,
        local_path=settings.storage.local_path,
        s3_bucket=settings.storage.s3_bucket,
        aws_region=settings.storage.aws_region,
    )

    filename = file.filename or "untitled"
    doc_row = await create_pending_document(
        session,
        storage,
        knowledge_base_id=kb_id,
        filename=filename,
        content_type=content_type,
        data=data,
    )
    # Commit the 'processing' row before the background task opens its own
    # session — otherwise its ingestion wouldn't see the row.
    await session.commit()
    doc_row = await knowledge_repo.get_document_by_id(session, doc_row.id)

    from turncall.storage.database import create_session_factory, get_engine

    ingest_in_background(
        create_session_factory(get_engine()),
        document_id=doc_row.id,
        knowledge_base_id=kb_id,
        filename=filename,
        content_type=content_type,
        data=data,
        openai_api_key=settings.openai.api_key,
        embedding_model=kb_row.embedding_model,
        chunk_size=kb_row.chunk_size,
        chunk_overlap=kb_row.chunk_overlap,
    )
    return ok(DocumentResponse.from_row(doc_row))


@router.get("/{kb_id}/documents")
async def list_documents(
    kb_id: UUID,
    auth: Auth,
    session: DbSession,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """List documents in a knowledge base."""
    kb_row = await knowledge_repo.get_knowledge_base_by_id(
        session, kb_id, project_id=auth.project_id
    )
    if kb_row is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    offset = (page - 1) * limit
    rows = await knowledge_repo.list_documents(
        session, kb_id, limit=limit, offset=offset
    )
    total = await knowledge_repo.count_documents(session, kb_id)
    return paginated(
        data=[DocumentResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/{kb_id}/documents/{doc_id}")
async def get_document(
    kb_id: UUID,
    doc_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get a document by ID."""
    kb_row = await knowledge_repo.get_knowledge_base_by_id(
        session, kb_id, project_id=auth.project_id
    )
    if kb_row is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    doc_row = await knowledge_repo.get_document_by_id(
        session, doc_id, knowledge_base_id=kb_id
    )
    if doc_row is None:
        raise NotFoundError("Document", str(doc_id))
    return ok(DocumentResponse.from_row(doc_row))


@router.delete("/{kb_id}/documents/{doc_id}", status_code=204)
async def delete_document(
    kb_id: UUID,
    doc_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> None:
    """Delete a document and its chunks."""
    kb_row = await knowledge_repo.get_knowledge_base_by_id(
        session, kb_id, project_id=auth.project_id
    )
    if kb_row is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    doc_row = await knowledge_repo.get_document_by_id(
        session, doc_id, knowledge_base_id=kb_id
    )
    if doc_row is None:
        raise NotFoundError("Document", str(doc_id))

    # Chunks cascade-deleted via FK
    await knowledge_repo.delete_document(session, doc_id)
    await session.commit()


# --- Search ---


@router.post("/{kb_id}/search")
async def search_knowledge_base(
    kb_id: UUID,
    body: SearchRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Search a knowledge base (debug/development endpoint)."""
    kb_row = await knowledge_repo.get_knowledge_base_by_id(
        session, kb_id, project_id=auth.project_id
    )
    if kb_row is None:
        raise NotFoundError("KnowledgeBase", str(kb_id))

    settings = get_settings()
    result = await retrieve(
        session,
        query=body.query,
        knowledge_base_ids=[kb_id],
        top_k=body.top_k,
        similarity_threshold=body.similarity_threshold,
        openai_api_key=settings.openai.api_key,
        embedding_model=kb_row.embedding_model,
    )

    return ok(
        SearchResponse(
            results=[
                SearchResultItem(
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    content=c.content,
                    similarity=c.similarity,
                    chunk_index=c.chunk_index,
                    token_count=c.token_count,
                )
                for c in result.chunks
            ],
            query=result.query,
            total=len(result.chunks),
        )
    )


# --- Agent ↔ Knowledge Base Linkage ---


agent_kb_router = APIRouter(prefix="/agents", tags=["agents"])


@agent_kb_router.post("/{agent_id}/knowledge-bases", status_code=201)
async def link_knowledge_base(
    agent_id: UUID,
    body: LinkKnowledgeBaseRequest,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Link a knowledge base to an agent."""
    from turncall.storage.repositories import agent_repo

    agent_row = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if agent_row is None:
        raise NotFoundError("Agent", str(agent_id))

    kb_row = await knowledge_repo.get_knowledge_base_by_id(
        session, body.knowledge_base_id, project_id=auth.project_id
    )
    if kb_row is None:
        raise NotFoundError("KnowledgeBase", str(body.knowledge_base_id))

    row = await knowledge_repo.link_agent_knowledge_base(
        session,
        agent_id=agent_id,
        knowledge_base_id=body.knowledge_base_id,
        mode=body.mode,
        priority=body.priority,
        top_k=body.top_k,
        similarity_threshold=body.similarity_threshold,
        tool_description=body.tool_description,
    )
    await session.commit()
    return ok(AgentKnowledgeBaseResponse.from_row(row))


@agent_kb_router.get("/{agent_id}/knowledge-bases")
async def list_agent_knowledge_bases(
    agent_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """List knowledge bases linked to an agent."""
    from turncall.storage.repositories import agent_repo

    agent_row = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if agent_row is None:
        raise NotFoundError("Agent", str(agent_id))

    rows = await knowledge_repo.get_agent_knowledge_bases(session, agent_id)
    return ok([AgentKnowledgeBaseResponse.from_row(r) for r in rows])


@agent_kb_router.delete("/{agent_id}/knowledge-bases/{kb_id}", status_code=204)
async def unlink_knowledge_base(
    agent_id: UUID,
    kb_id: UUID,
    auth: Auth,
    session: DbSession,
) -> None:
    """Unlink a knowledge base from an agent."""
    removed = await knowledge_repo.unlink_agent_knowledge_base(
        session, agent_id=agent_id, knowledge_base_id=kb_id
    )
    if not removed:
        raise NotFoundError("AgentKnowledgeBase", f"{agent_id}/{kb_id}")
    await session.commit()
