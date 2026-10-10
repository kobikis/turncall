"""SMS/chat orchestration service.

Handles inbound SMS and programmatic chat messages:
- Session creation / resumption / expiry
- LLM message history assembly
- Response generation via llm_text
- Webhook event dispatch
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.errors import BadRequestError
from turncall.domain.enums import CallEventType, ChatChannel, SmsMessageRole
from turncall.domain.models import AgentConfig
from turncall.domain.session_state import is_session_expired
from turncall.events.dispatcher import dispatch_event
from turncall.services.chat_tools import build_chat_tools
from turncall.services.limits import admit_message
from turncall.services.llm_text import complete_text
from turncall.services.template_renderer import (
    check_variables,
    prepend_knowledge_context,
    render_agent_config,
)
from turncall.services.text_agent import TextAgent, resolve_text_agent
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

    agent = await resolve_text_agent(
        db, phone_row, customer_number=from_number, channel=ChatChannel.SMS
    )
    return await process_inbound_text(
        db,
        agent,
        project_id=phone_row.project_id,
        phone_number_id=phone_row.id,
        customer_number=from_number,
        turncall_number=to_number,
        message=body,
        channel=ChatChannel.SMS,
        provider_message_sid=provider_message_sid,
    )


async def process_inbound_text(
    db: AsyncSession,
    agent: TextAgent,
    *,
    project_id: UUID,
    phone_number_id: UUID,
    customer_number: str,
    turncall_number: str,
    message: str,
    channel: ChatChannel,
    provider_message_sid: str | None,
) -> ChatResult:
    """A text to one of our numbers, on the agent `resolve_text_agent` chose:
    continuing its session, or opening the one call-init answered for."""
    return await _process_chat_message(
        db,
        project_id=project_id,
        agent_id=agent.agent_id,
        agent_config=agent.config,
        phone_number_id=phone_number_id,
        customer_number=customer_number,
        turncall_number=turncall_number,
        message=message,
        channel=channel,
        provider_message_sid=provider_message_sid,
        existing_session_id=agent.session_id,
        new_session_id=agent.new_session_id,
        new_session_metadata=agent.session_metadata,
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
    variables: dict[str, str] | None = None,
) -> ChatResult:
    """Handle a programmatic chat message (API/web channel).

    `variables` are merged into the session's `{{variables}}`, a name sent
    again replacing its value; a message without them leaves them as they are.
    """
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
        variables=variables,
    )


async def _process_chat_message(
    db: AsyncSession,
    *,
    project_id: UUID,
    agent_id: UUID | None,
    agent_config: AgentConfig,
    phone_number_id: UUID | None,
    customer_number: str,
    turncall_number: str,
    message: str,
    channel: ChatChannel,
    provider_message_sid: str | None = None,
    existing_session_id: UUID | None = None,
    new_session_id: UUID | None = None,
    new_session_metadata: dict | None = None,
    variables: dict[str, str] | None = None,
) -> ChatResult:
    """Core chat processing: session management, LLM call, event dispatch.

    `agent_id` is None for an inline agent (ADR-0017). A session created here
    takes `new_session_id` and `new_session_metadata` — what call-init
    answered for it.
    """
    # OpenRouter is voice-only for v1 — reject customer text conversations before
    # any session/message side effects. (Internal callers like post-call analysis
    # use complete_text directly and are allowed.) See ADR-0003.
    if agent_config.llm.provider == "openrouter":
        raise ValueError(
            "openrouter provider is voice-only (not supported for SMS/chat)"
        )

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
        new_session_id=new_session_id,
        new_session_metadata=new_session_metadata,
    )

    merged = _merged_variables(session_row, variables) if variables else None

    # Over the session's messages per minute (#226): raised before anything is
    # stored or sent to the LLM. The Chat API answers 429; SMS and WhatsApp
    # catch it and send nothing.
    await admit_message(db, project_id=project_id, session_id=session_row.id)
    if merged is not None:
        session_row = await _remember_variables(db, session_row, merged)

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
    agent_config = for_session(agent_config, session_row.metadata_json or {})
    system_prompt = agent_config.system_prompt

    # The project's keys, so a text turn runs on the account a voice call on
    # the same project would (ADR-0020): agent inline, project, platform.
    from turncall.services.credentials import for_project

    credentials = await for_project(db, project_id)

    # The agent's knowledge bases: `prompt` and `auto` go into the prompt
    # here, `tool` becomes `query_knowledge` with the turn's other tools.
    attachments = await _kb_attachments(db, project_id, agent_id, agent_config)
    system_prompt = await _inject_kb_context(
        db,
        attachments=attachments,
        system_prompt=system_prompt,
        user_message=message,
        openai_api_key=credentials.api_key("openai"),
        tool_names=[t.name for t in agent_config.tools],
    )

    llm_messages = await _build_llm_messages(db, session_row.id, system_prompt)

    # Webhook + MCP tools for this turn. The tool_calls turn and its results
    # live inside complete_text only — history is rebuilt from sms_messages
    # each turn, which stores the customer/assistant text and nothing else.
    chat_tools = await build_chat_tools(
        agent_config,
        session_id=session_row.id,
        project_id=project_id,
        knowledge=[a for a in attachments if a.mode == "tool"],
        openai_api_key=credentials.api_key("openai"),
    )
    try:
        completion = await complete_text(
            agent_config.llm,
            llm_messages,
            aws=agent_config.aws,
            tools=chat_tools.schemas or None,
            execute_tool=chat_tools.execute,
            credentials=credentials,
        )
    finally:
        await chat_tools.aclose()

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
    agent_id: UUID | None,
    phone_number_id: UUID | None,
    customer_number: str,
    turncall_number: str,
    channel: str,
    existing_session_id: UUID | None = None,
    new_session_id: UUID | None = None,
    new_session_metadata: dict | None = None,
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
        metadata_json=new_session_metadata,
        session_id=new_session_id,
    )
    return new_session, True


def _merged_variables(
    session_row: SmsSessionRow, variables: dict[str, str]
) -> dict[str, str]:
    """`variables` over the session's, bounded as one request's are. Checked
    before the message is admitted, so a refused one costs no rate slot."""
    stored = (session_row.metadata_json or {}).get("template_variables")
    merged = {**(stored if isinstance(stored, dict) else {}), **variables}
    try:
        return check_variables(merged)
    except ValueError as exc:
        raise BadRequestError(f"the session's {exc}") from exc


async def _remember_variables(
    db: AsyncSession, session_row: SmsSessionRow, merged: dict[str, str]
) -> SmsSessionRow:
    """Store the session's merged `{{variables}}`, when they changed."""
    metadata = session_row.metadata_json or {}
    if merged == metadata.get("template_variables"):
        return session_row
    updated = await sms_session_repo.set_session_metadata(
        db, session_row.id, {**metadata, "template_variables": merged}
    )
    return updated or session_row


def for_session(config: AgentConfig, metadata: dict) -> AgentConfig:
    """The agent as this session runs it: `{{variables}}` rendered, then
    call-init's `knowledge_context` prepended — a call's order. The agent's
    own `metadata` fills any name the session does not set."""
    stored = metadata.get("template_variables")
    variables = {
        **{k: str(v) for k, v in config.metadata.items()},
        **(stored if isinstance(stored, dict) else {}),
    }
    rendered = render_agent_config(config, variables)
    knowledge_context = metadata.get("knowledge_context")
    if not isinstance(knowledge_context, str):
        return rendered
    return prepend_knowledge_context(rendered, knowledge_context)


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


async def _kb_attachments(
    db: AsyncSession, project_id: UUID, agent_id: UUID | None, config: AgentConfig
) -> list:
    """The agent's knowledge-base links: a stored agent's rows, or an inline
    agent's own `knowledge_bases` (#260)."""
    from turncall.storage.repositories import knowledge_repo

    if agent_id is not None:
        return list(await knowledge_repo.get_agent_knowledge_bases(db, agent_id))
    return await _inline_attachments(db, project_id, config)


async def _inject_kb_context(
    db: AsyncSession,
    *,
    attachments: list,
    system_prompt: str,
    user_message: str,
    openai_api_key: str,
    tool_names: Iterable[str] = (),
) -> str:
    """Inject knowledge base context into system prompt for chat.

    - prompt: Full document text prepended
    - auto: Retrieval based on user message, context prepended
    - tool: nothing here — `query_knowledge` is offered with the turn's tools
      (`chat_tools`); the awareness hint covers it, as on a voice call
    """
    from turncall.services.retrieval import (
        format_retrieved_context,
        get_full_text_context,
        knowledge_hints,
        retrieve,
    )

    if not attachments:
        return system_prompt

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
            openai_api_key=openai_api_key,
        )
        if result.chunks:
            context_text = format_retrieved_context(result)
            system_prompt = f"{context_text}\n\n{system_prompt}"

    # Tell the model it has a KB even when this message retrieved nothing, so it
    # doesn't deny having one (e.g. "use the attached file" matches nothing),
    # and to search a `tool`-mode one before saying no — as
    # build_knowledge_preamble does on a voice call.
    hints = knowledge_hints({a.mode for a in attachments}, tool_names)
    if hints:
        system_prompt = f"{system_prompt}\n\n{hints}"

    return system_prompt


async def _inline_attachments(
    db: AsyncSession, project_id: UUID, config: AgentConfig
) -> list:
    """An inline agent's knowledge bases (#260), each resolved against the
    session's project: one from elsewhere, or gone, is skipped with a warning
    and the reply goes out without it."""
    from turncall.storage.repositories import knowledge_repo

    usable = []
    # ponytail: one lookup per KB — an agent names a handful.
    for att in sorted(config.knowledge_bases, key=lambda a: a.priority):
        kb = await knowledge_repo.get_knowledge_base_by_id(
            db, att.knowledge_base_id, project_id=project_id
        )
        if kb is None:
            logger.warning(
                "knowledge_base_skipped: {kb} is not a knowledge base in project "
                "{project}",
                kb=str(att.knowledge_base_id),
                project=str(project_id),
            )
            continue
        usable.append(att)
    return usable
