"""WhatsApp text message handling and outbound reply sender.

Handles inbound WhatsApp text messages by reusing the shared chat
processing pipeline and sends replies via the WhatsApp Cloud API.
"""

from __future__ import annotations

from typing import Any

import aiohttp
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import ChatChannel
from turncall.domain.models import AgentConfig
from turncall.services.sms_chat import ChatResult, _process_chat_message
from turncall.storage.models import AgentRow, PhoneNumberRow
from turncall.storage.repositories.agent_repo import get_agent_by_id
from turncall.storage.repositories.phone_number_repo import get_by_e164

WHATSAPP_API_BASE = "https://graph.facebook.com/v23.0"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def handle_inbound_whatsapp(
    db: AsyncSession,
    *,
    from_number: str,
    to_number: str,
    body: str,
    provider_message_id: str | None = None,
) -> ChatResult:
    """Handle an inbound WhatsApp text message.

    Resolves the phone number -> agent, manages session lifecycle,
    generates an LLM reply, and dispatches webhook events.
    """
    phone_row = await get_by_e164(db, to_number)
    if phone_row is None:
        raise ValueError(f"Phone number not found: {to_number}")
    if not phone_row.whatsapp_enabled:
        raise ValueError(f"WhatsApp not enabled on number: {to_number}")

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
        channel=ChatChannel.WHATSAPP,
        provider_message_sid=provider_message_id,
    )


async def send_whatsapp_text(
    http_session: aiohttp.ClientSession,
    *,
    token: str,
    phone_number_id: str,
    to: str,
    text: str,
) -> dict[str, Any]:
    """Send a text message via WhatsApp Cloud API.

    Returns the API response dict. Raises ValueError on API errors.
    """
    url = f"{WHATSAPP_API_BASE}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with http_session.post(
        url, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT
    ) as resp:
        result: dict[str, Any] = await resp.json()
        if resp.status >= 400:
            error_msg = result.get("error", {}).get("message", "Unknown error")
            logger.error(
                "whatsapp_send_failed",
                status=resp.status,
                error=error_msg,
                to=to,
            )
            raise ValueError(f"WhatsApp API error {resp.status}: {error_msg}")

        logger.info("whatsapp_message_sent", to=to)
        return result


async def _resolve_agent(
    db: AsyncSession, phone_row: PhoneNumberRow
) -> AgentRow | None:
    """Resolve the agent for a phone number's routing target."""
    if phone_row.routing_target_type == "agent" and phone_row.routing_target_id:
        return await get_agent_by_id(db, phone_row.routing_target_id)
    return None
