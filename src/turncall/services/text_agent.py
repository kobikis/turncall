"""Which agent answers an inbound text, and with what (SMS, WhatsApp text).

A number routed to an agent answers with that agent. A number routed to
call-init (`routing_target_type: "webhook"`) asks its server once per
conversation — on the message that opens a session — and the session keeps
the answer in `metadata_json`, under the keys a call row uses: the inline
agent (`dynamic_config`, ADR-0017; the session's `agent_id` is then null),
`template_variables`, `metadata` and `knowledge_context`. Every later message
of that session runs on what was stored, without asking again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import ChatChannel
from turncall.domain.models import AgentConfig
from turncall.domain.session_state import is_session_expired
from turncall.events.server_events import send_call_init
from turncall.services.call_init_resolver import resolve_call_init, usable_agent
from turncall.storage.models import PhoneNumberRow, SmsSessionRow
from turncall.storage.repositories import sms_session_repo
from turncall.storage.repositories.agent_repo import get_agent_by_id

DYNAMIC_CONFIG = "dynamic_config"

# `call.type` on a text session's call-init, beside the voice ones.
CALL_INIT_TYPES = {
    ChatChannel.SMS: "inboundSms",
    ChatChannel.WHATSAPP: "inboundWhatsAppMessage",
}


@dataclass(frozen=True)
class TextAgent:
    """The agent a message runs on, and the session it belongs to.

    `session_id` is the session the message continues. When call-init just
    ran, `new_session_id` is the id it was told, for the session to be created
    with `session_metadata`.
    """

    agent_id: UUID | None
    config: AgentConfig
    session_id: UUID | None = None
    new_session_id: UUID | None = None
    session_metadata: dict[str, Any] = field(default_factory=dict)


async def resolve_text_agent(
    db: AsyncSession,
    number: PhoneNumberRow,
    *,
    customer_number: str,
    channel: ChatChannel,
) -> TextAgent:
    """The agent for a text to `number`; ValueError when there is none."""
    if number.routing_target_type == "webhook" and number.server_url:
        session = await sms_session_repo.get_active_session(
            db, customer_number, number.e164_number
        )
        if session is not None and not is_session_expired(session.expires_at):
            return TextAgent(
                session.agent_id, await session_config(db, session), session.id
            )
        return await _ask_call_init(db, number, customer_number, channel)

    if number.routing_target_type == "agent" and number.routing_target_id:
        agent = await get_agent_by_id(db, number.routing_target_id)
        if agent is not None:
            return TextAgent(agent.id, AgentConfig.model_validate(agent.config_blob))
    raise ValueError(f"No agent found for number: {number.e164_number}")


async def session_config(db: AsyncSession, session: SmsSessionRow) -> AgentConfig:
    """The config a session's agent runs on: its row's, or its inline one."""
    if session.agent_id is not None:
        agent = await get_agent_by_id(db, session.agent_id)
        if agent is None:
            raise ValueError(f"Agent not found: {session.agent_id}")
        return AgentConfig.model_validate(agent.config_blob)
    inline = (session.metadata_json or {}).get(DYNAMIC_CONFIG)
    if not isinstance(inline, dict):
        raise ValueError(f"Session {session.id} has no agent")
    return AgentConfig.model_validate(inline)


async def _ask_call_init(
    db: AsyncSession,
    number: PhoneNumberRow,
    customer_number: str,
    channel: ChatChannel,
) -> TextAgent:
    """Run call-init for a new conversation. The server answers for this
    session the way it answers for a call: an agent id in the number's
    project or an inline config, plus what the session should carry."""
    session_id = uuid4()
    response = await send_call_init(
        number.server_url,
        call_id=str(session_id),
        from_number=customer_number,
        to_number=number.e164_number,
        transport_type=CALL_INIT_TYPES[channel],
        secret=number.server_url_secret,
    )
    chosen = None
    if response.success and isinstance(response.data, dict):
        result = await resolve_call_init(db, response.data)
        chosen = usable_agent(response.data, result, number.project_id, default=None)
    if chosen is None or chosen == (None, None):
        logger.warning(
            "text_call_init_no_agent: message unanswered",
            server_url=number.server_url,
            error=response.error,
        )
        raise ValueError(f"call-init named no agent for number: {number.e164_number}")

    agent_id, inline = chosen
    config = AgentConfig.model_validate(inline or result.agent.config_blob)
    stored = {
        DYNAMIC_CONFIG: inline,
        "template_variables": result.template_variables,
        "metadata": result.metadata,
        "knowledge_context": result.knowledge_context,
    }
    return TextAgent(
        agent_id,
        config,
        new_session_id=session_id,
        session_metadata={k: v for k, v in stored.items() if v},
    )
