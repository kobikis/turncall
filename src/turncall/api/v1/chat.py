"""Chat API endpoints for SMS/web/API text conversations."""

from uuid import UUID

from fastapi import APIRouter

from turncall.api.deps import DbSession
from turncall.api.errors import NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.chat import (
    ChatMessageResponse,
    ChatSessionResponse,
    SendChatMessageRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.services.sms_chat import handle_chat_message
from turncall.storage.repositories import sms_message_repo, sms_session_repo

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", status_code=201)
async def send_message(
    body: SendChatMessageRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Send a chat message and receive an LLM reply.

    Creates or resumes a session. Supports context threading
    via session_id or previous_chat_id.
    """
    result = await handle_chat_message(
        session,
        session_id=body.session_id,
        previous_chat_id=body.previous_chat_id,
        agent_id=body.agent_id,
        project_id=auth.project_id,
        message=body.message,
        channel=body.channel,
        customer_number=body.customer_number,
        turncall_number=body.turncall_number,
    )

    return ok(
        {
            "session_id": str(result.session_id),
            "message_id": str(result.message_id),
            "reply": result.reply_text,
            "is_new_session": result.is_new_session,
        }
    )


@router.get("/sessions")
async def list_sessions(
    auth: Auth,
    session: DbSession,
    status: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """List chat sessions for the authenticated project."""
    offset = (page - 1) * limit
    rows = await sms_session_repo.list_sessions(
        session, auth.project_id, status=status, limit=limit, offset=offset
    )
    total = await sms_session_repo.count_sessions(
        session, auth.project_id, status=status
    )
    return paginated(
        data=[ChatSessionResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/sessions/{session_id}")
async def get_session_detail(
    session_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get a chat session with details."""
    row = await sms_session_repo.get_session_by_id(
        session, session_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("ChatSession", str(session_id))
    return ok(ChatSessionResponse.from_row(row))


@router.get("/sessions/{session_id}/messages")
async def list_session_messages(
    session_id: UUID,
    auth: Auth,
    session: DbSession,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List messages in a chat session."""
    # Verify session belongs to project
    sess_row = await sms_session_repo.get_session_by_id(
        session, session_id, project_id=auth.project_id
    )
    if sess_row is None:
        raise NotFoundError("ChatSession", str(session_id))

    rows = await sms_message_repo.list_messages_for_session(
        session, session_id, limit=limit, offset=offset
    )
    total = await sms_message_repo.count_messages_for_session(session, session_id)
    return paginated(
        data=[ChatMessageResponse.from_row(r) for r in rows],
        total=total,
        page=1,
        limit=limit,
    )


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Expire/delete a chat session."""
    row = await sms_session_repo.get_session_by_id(
        session, session_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("ChatSession", str(session_id))

    await sms_session_repo.expire_session(session, session_id)
    return ok({"deleted": True})
