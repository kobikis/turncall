"""SMS/chat orchestration service.

Handles inbound SMS and programmatic chat messages:
- Session creation / resumption / expiry
- LLM message history assembly
- Response generation via llm_text
- Webhook event dispatch
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import CallEventType, ChatChannel, SmsMessageRole
from turncall.domain.models import AgentConfig
from turncall.domain.session_state import is_session_expired
from turncall.events.dispatcher import dispatch_event
from turncall.services.llm_text import complete_text
from turncall.services.template_renderer import render_template
from turncall.storage.models import SmsSessionRow
from turncall.storage.repositories import sms_message_repo, sms_session_repo
from turncall.storage.repositories.agent_repo import get_agent_by_id
from turncall.storage.repositories.phone_number_repo import get_by_e164


@dataclass(frozen=True)
class ChatResult:
    """Result of a chat interaction."""

    success: bool
    session_id: UUID
    message_id: UUID
    reply_text: str
    is_new_session: bool
    error: str | None = None


async def handle_inbound_sms(
    db: AsyncSession,
    *,
    from_number: str,
    to_number: str,
    body: str,
    provider_message_sid: str | None = None,
) -> ChatResult:
    """Handle an inbound SMS message.

    Resolves the phone number → agent, manages session lifecycle,
    generates an LLM reply, and dispatches webhook events.
    """
    phone_row = await get_by_e164(db, to_number)
    if phone_row is None:
        raise ValueError(f"Phone number not found: {to_number}")
    if not phone_row.sms_enabled:
        raise ValueError(f"SMS not enabled on number: {to_number}")

    # Resolve agent
    agent_row = await _resolve_agent(db, phone_row)
    if agent_row is None:
        raise ValueError(f"No agent found for number: {to_number}")

    config = AgentConfig.model_validate(agent_row.config_blob)

    return await _process_chat_message(
        db,
        project_id=phone_row.project_id,
        agent_id=agent_row.id,
        agent_config=config,
        phone_number_id=phone_row.id,
        customer_number=from_number,
        turncall_number=to_number,
        message=body,
        channel=ChatChannel.SMS,
        provider_message_sid=provider_message_sid,
    )


async def handle_chat_message(
    db: AsyncSession,
    *,
    session_id: UUID | None = None,
    previous_chat_id: UUID | None = None,
    agent_id: UUID,
    project_id: UUID,
    message: str,
    channel: str = "api",
    customer_number: str | None = None,
    turncall_number: str | None = None,
) -> ChatResult:
    """Handle a programmatic chat message (API/web channel)."""
    # Resolve session from previous_chat_id if provided
    if previous_chat_id is not None and session_id is None:
        prev_message = await sms_message_repo.get_message_by_id(db, previous_chat_id)
        if prev_message is not None:
            session_id = prev_message.session_id

    # Resolve agent
    agent_row = await get_agent_by_id(db, agent_id, project_id=project_id)
    if agent_row is None:
        raise ValueError(f"Agent not found: {agent_id}")

    config = AgentConfig.model_validate(agent_row.config_blob)

    return await _process_chat_message(
        db,
        project_id=project_id,
        agent_id=agent_id,
        agent_config=config,
        phone_number_id=None,
        customer_number=customer_number or "api",
        turncall_number=turncall_number or "api",
        message=message,
        channel=ChatChannel(channel),
        provider_message_sid=None,
        existing_session_id=session_id,
    )


async def _process_chat_message(
    db: AsyncSession,
    *,
    project_id: UUID,
    agent_id: UUID,
    agent_config: AgentConfig,
    phone_number_id: UUID | None,
    customer_number: str,
    turncall_number: str,
    message: str,
    channel: ChatChannel,
    provider_message_sid: str | None = None,
    existing_session_id: UUID | None = None,
) -> ChatResult:
    """Core chat processing: session management, LLM call, event dispatch."""
    # OpenRouter is voice-only for v1 — reject customer text conversations before
    # any session/message side effects. (Internal callers like post-call analysis
    # use complete_text directly and are allowed.) See ADR-0003.
    if agent_config.llm.provider == "openrouter":
        raise ValueError("openrouter provider is voice-only (not supported for SMS/chat)")

    # 1. Resolve or create session
    session_row, is_new = await _resolve_or_create_session(
        db,
        project_id=project_id,
        agent_id=agent_id,
        phone_number_id=phone_number_id,
        customer_number=customer_number,
        turncall_number=turncall_number,
        channel=channel.value,
        existing_session_id=existing_session_id,
    )

    # 2. Store the customer message
    customer_msg = await sms_message_repo.create_message(
        db,
        session_id=session_row.id,
        project_id=project_id,
        role=SmsMessageRole.CUSTOMER,
        content=message,
        channel=channel.value,
        provider_message_sid=provider_message_sid,
    )

    # Commit the inbound side before emitting its webhooks — a later failure
    # (KB injection, LLM error) must not roll these rows back after subscribers
    # already received session.created/chat.created for IDs that never persisted.
    await db.commit()

    # 3. Dispatch events (rows are now durable)
    if is_new:
        await _dispatch_session_event(
            db, CallEventType.SESSION_CREATED, session_row, project_id
        )

    await _dispatch_chat_event(db, customer_msg, session_row.id, project_id)

    # 4. Build LLM messages and generate reply
    system_prompt = agent_config.system_prompt
    if agent_config.metadata:
        system_prompt = render_template(
            system_prompt, {k: str(v) for k, v in agent_config.metadata.items()}
        )

    # Inject knowledge base context if agent has KBs attached
    system_prompt = await _inject_kb_context(
        db,
        agent_id=agent_id,
        agent_config=agent_config,
        system_prompt=system_prompt,
        user_message=message,
    )

    llm_messages = await _build_llm_messages(db, session_row.id, system_prompt)
    completion = await complete_text(
        agent_config.llm, llm_messages, aws=agent_config.aws
    )

    # 5. Store agent reply
    agent_msg = await sms_message_repo.create_message(
        db,
        session_id=session_row.id,
        project_id=project_id,
        role=SmsMessageRole.ASSISTANT,
        content=completion.text,
        channel=channel.value,
        token_count=completion.total_tokens,
    )

    # 6. Update session activity
    await sms_session_repo.update_session_activity(db, session_row.id)
    await db.commit()

    # 7. Dispatch agent message event
    await _dispatch_chat_event(db, agent_msg, session_row.id, project_id)
    await _dispatch_session_event(
        db, CallEventType.SESSION_UPDATED, session_row, project_id
    )

    logger.info(
        "chat_message_processed",
        session_id=str(session_row.id),
        channel=channel.value,
        is_new_session=is_new,
        reply_length=len(completion.text),
    )

    return ChatResult(
        success=True,
        session_id=session_row.id,
        message_id=agent_msg.id,
        reply_text=completion.text,
        is_new_session=is_new,
    )


async def _resolve_or_create_session(
    db: AsyncSession,
    *,
    project_id: UUID,
    agent_id: UUID,
    phone_number_id: UUID | None,
    customer_number: str,
    turncall_number: str,
    channel: str,
    existing_session_id: UUID | None = None,
) -> tuple[SmsSessionRow, bool]:
    """Find an active session or create a new one.

    Returns (session_row, is_new_session).
    """
    # If caller specified a session ID, try to use it
    if existing_session_id is not None:
        row = await sms_session_repo.get_session_by_id(
            db, existing_session_id, project_id=project_id
        )
        if (
            row is not None
            and row.status == "active"
            and not is_session_expired(row.expires_at)
        ):
            return row, False
        # Session expired or not found — fall through to create new

    # Look up by phone number pair (for SMS channel)
    if customer_number != "api":
        existing = await sms_session_repo.get_active_session(
            db, customer_number, turncall_number
        )
        if existing is not None:
            if not is_session_expired(existing.expires_at):
                return existing, False
            # Expire the stale session
            await sms_session_repo.expire_session(db, existing.id)

    # Create a new session
    new_session = await sms_session_repo.create_session(
        db,
        project_id=project_id,
        agent_id=agent_id,
        phone_number_id=phone_number_id,
        customer_number=customer_number,
        turncall_number=turncall_number,
        channel=channel,
    )
    return new_session, True


async def _build_llm_messages(
    db: AsyncSession,
    session_id: UUID,
    system_prompt: str,
) -> list[dict[str, str]]:
    """Build the LLM message history from session messages."""
    messages: list[dict[str, str]] = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    rows = await sms_message_repo.list_messages_for_session(db, session_id)
    for row in rows:
        role = "user" if row.role == SmsMessageRole.CUSTOMER else "assistant"
        if row.role == SmsMessageRole.SYSTEM:
            role = "system"
        messages.append({"role": role, "content": row.content})

    return messages


async def _resolve_agent(db: AsyncSession, phone_row: object) -> object | None:
    """Resolve the agent for a phone number's routing target."""
    if phone_row.routing_target_type == "agent" and phone_row.routing_target_id:
        return await get_agent_by_id(db, phone_row.routing_target_id)
    return None


async def _dispatch_session_event(
    db: AsyncSession,
    event_type: CallEventType,
    session_row: SmsSessionRow,
    project_id: UUID,
) -> None:
    """Dispatch a session lifecycle webhook event."""
    await dispatch_event(
        db,
        project_id=project_id,
        event_type=event_type.value,
        payload={
            "session_id": str(session_row.id),
            "customer_number": session_row.customer_number,
            "turncall_number": session_row.turncall_number,
            "channel": session_row.channel,
            "status": session_row.status,
        },
        session_id=session_row.id,
        agent_id=session_row.agent_id,
    )


async def _dispatch_chat_event(
    db: AsyncSession,
    message_row: object,
    session_id: UUID,
    project_id: UUID,
) -> None:
    """Dispatch a chat.created webhook event."""
    await dispatch_event(
        db,
        project_id=project_id,
        event_type=CallEventType.CHAT_CREATED.value,
        payload={
            "message_id": str(message_row.id),
            "session_id": str(session_id),
            "role": message_row.role,
            "content": message_row.content,
            "channel": message_row.channel,
        },
        session_id=session_id,
    )


async def _inject_kb_context(
    db: AsyncSession,
    *,
    agent_id: UUID,
    agent_config: AgentConfig,
    system_prompt: str,
    user_message: str,
) -> str:
    """Inject knowledge base context into system prompt for chat.

    Handles all three modes:
    - prompt: Full document text prepended
    - auto: Retrieval based on user message, context prepended
    - tool: Skipped (tool mode not supported in text chat)
    """
    from turncall.config.settings import get_settings
    from turncall.services.retrieval import (
        format_retrieved_context,
        get_full_text_context,
        retrieve,
    )
    from turncall.storage.repositories import knowledge_repo

    attachments = await knowledge_repo.get_agent_knowledge_bases(db, agent_id)
    if not attachments:
        return system_prompt

    settings = get_settings()

    # Prompt mode: inject full document text
    prompt_kb_ids = [a.knowledge_base_id for a in attachments if a.mode == "prompt"]
    if prompt_kb_ids:
        full_text = await get_full_text_context(db, prompt_kb_ids)
        if full_text:
            system_prompt = f"{full_text}\n\n{system_prompt}"

    # Auto mode: retrieve based on user message
    auto_kb_ids = [a.knowledge_base_id for a in attachments if a.mode == "auto"]
    if auto_kb_ids:
        first_auto = next(a for a in attachments if a.mode == "auto")
        result = await retrieve(
            db,
            query=user_message,
            knowledge_base_ids=auto_kb_ids,
            top_k=first_auto.top_k,
            similarity_threshold=first_auto.similarity_threshold,
            openai_api_key=settings.openai.api_key,
        )
        if result.chunks:
            context_text = format_retrieved_context(result)
            system_prompt = f"{context_text}\n\n{system_prompt}"
        # Tell the model it has a KB even when this message didn't retrieve, so it
        # doesn't deny having one (e.g. "use the attached file" matches nothing).
        from turncall.services.retrieval import KNOWLEDGE_AWARENESS_HINT

        system_prompt = f"{system_prompt}\n\n{KNOWLEDGE_AWARENESS_HINT}"

    return system_prompt
