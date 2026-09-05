"""Transfer state + briefing resolution for warm/cold call transfer.

The transfer intent is parked in Redis when a transfer is issued so the
Twilio callbacks (whisper, result, AMD) can read it when they fire a moment
later. See ADR-0009.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

TRANSFER_TTL_SECONDS = 300


def _redis_key(call_id: UUID) -> str:
    return f"transfer:{call_id}"


@dataclass(frozen=True)
class TransferIntent:
    """The transient details of an in-flight transfer (Redis-backed)."""

    target_number: str
    transfer_mode: str
    transfer_message: str | None = None
    briefing_text: str | None = None
    briefing_from_summary: bool = False
    fallback_message: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> TransferIntent:
        return cls(**json.loads(raw))


def normalize_briefing(briefing: object) -> tuple[str | None, bool]:
    """Split a briefing value into (static_text, from_summary).

    Accepts a string, a {"from_summary": bool} mapping, a pydantic
    BriefingSummary, or None.
    """
    if briefing is None:
        return None, False
    if isinstance(briefing, str):
        return briefing, False
    from_summary = getattr(briefing, "from_summary", None)
    if from_summary is None and isinstance(briefing, dict):
        from_summary = briefing.get("from_summary", False)
    return None, bool(from_summary)


def transfer_callback_urls(base_url: str, call_id: UUID) -> dict[str, str]:
    """Build the absolute Twilio-callback URLs for a transfer."""
    base = base_url.rstrip("/")
    return {
        "whisper": f"{base}/webhooks/twilio/transfer-whisper/{call_id}",
        "result": f"{base}/webhooks/twilio/transfer-result/{call_id}",
        "amd": f"{base}/webhooks/twilio/transfer-amd/{call_id}",
    }


async def store_transfer_intent(call_id: UUID, intent: TransferIntent) -> None:
    """Park the transfer intent in Redis with a short TTL."""
    from turncall.storage.redis import get_redis

    await get_redis().setex(_redis_key(call_id), TRANSFER_TTL_SECONDS, intent.to_json())


async def load_transfer_intent(call_id: UUID) -> TransferIntent | None:
    """Read the transfer intent back (None if expired / never set)."""
    from turncall.storage.redis import get_redis

    raw = await get_redis().get(_redis_key(call_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return TransferIntent.from_json(raw)
    except (ValueError, TypeError):
        logger.warning("transfer_intent_decode_failed", call_id=str(call_id))
        return None


_SUMMARY_SYSTEM = (
    "You are briefing a human agent who is about to take over a live phone call. "
    "In two or three sentences, state who the caller is and what they need, so the "
    "agent can pick up instantly. Be terse and factual; no greeting, no sign-off."
)


async def resolve_briefing_text(
    session: AsyncSession,
    call_id: UUID,
    intent: TransferIntent,
) -> str | None:
    """The text to speak to the operator: static briefing or a live summary."""
    if not intent.briefing_from_summary:
        return intent.briefing_text

    # Auto-summary: gather the transcript and run the agent's LLM on the fly.
    from turncall.domain.models import AWSConfig, LLMConfig
    from turncall.services.llm_text import complete_text
    from turncall.storage.repositories import agent_repo, call_repo

    events = await call_repo.list_call_events(
        session, call_id, event_type="transcript.final", limit=200
    )
    lines = []
    for e in events:
        payload = e.payload or {}
        speaker = payload.get("role") or payload.get("user_id")
        role = "Agent" if speaker == "assistant" else "Caller"
        text = (payload.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    transcript = "\n".join(lines) or "(no transcript captured)"

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None or call.active_agent_id is None:
        return "The caller is being transferred to you."
    agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)
    if agent is None:
        return "The caller is being transferred to you."

    llm_config = LLMConfig(**(agent.config_blob.get("llm") or {}))
    aws_config = AWSConfig(**(agent.config_blob.get("aws") or {}))
    try:
        result = await complete_text(
            llm_config,
            [
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": f"Transcript:\n{transcript}"},
            ],
            aws=aws_config,
        )
        return result.text.strip() or "The caller is being transferred to you."
    except Exception:
        logger.exception("transfer_briefing_summary_failed", call_id=str(call_id))
        return "The caller is being transferred to you."
