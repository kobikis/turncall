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
from turncall.services.sms_chat import ChatResult, process_inbound_text
from turncall.services.text_agent import resolve_text_agent
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

    agent = await resolve_text_agent(
        db, phone_row, customer_number=from_number, channel=ChatChannel.WHATSAPP
    )
    return await process_inbound_text(
        db,
        agent,
        project_id=phone_row.project_id,
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
