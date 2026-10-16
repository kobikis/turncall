"""Transfer state + briefing resolution for warm/cold call transfer.

The transfer intent is parked in Redis when a transfer is issued so the
carrier's callbacks (Twilio's whisper, result and AMD; every Call Control
webhook of a Telnyx transfer) can read it when they fire a moment later.
See ADR-0009.
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
    # Telnyx: the operator's leg, once it is ringing (services/telnyx_transfer).
    operator_leg: str | None = None

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


async def clear_transfer_intent(call_id: UUID) -> None:
    """Forget a transfer that did not happen."""
    from turncall.storage.redis import get_redis

    await get_redis().delete(_redis_key(call_id))


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
    *,
    here: bool = False,
) -> str | None:
    """The text to speak to the operator: static briefing or a live summary.

    `here`: build it in this process whoever holds the conversation (the
    holder answering another replica, `briefing_here`).
    """
    if not intent.briefing_from_summary:
        return intent.briefing_text

    # Auto-summary: gather the transcript and run the agent's LLM on the fly.
    from turncall.domain.models import AWSConfig, LLMConfig
    from turncall.services import live_transcript, redaction
    from turncall.services.llm_text import complete_text
    from turncall.storage.repositories import agent_repo, call_repo

    call = await call_repo.get_call_by_id(session, call_id)
    if (
        not here
        and call is not None
        and live_transcript.lines(call_id) is None
        and await redaction.policy_for_project(call.project_id, session=session)
    ):
        # The whisper landed on a replica that did not run the call, and the
        # stored transcript is redacted: the process holding the conversation
        # builds the briefing (services/live_transcript).
        answered = await live_transcript.ask_holder(call_id, live_transcript.BRIEFING)
        if answered is not None and answered.get("text"):
            return str(answered["text"])

    lines = []
    for speaker, said in await _conversation(session, call_id):
        role = "Agent" if speaker == "assistant" else "Caller"
        text = said.strip()
        if text:
            lines.append(f"{role}: {text}")
    transcript = "\n".join(lines) or "(no transcript captured)"

    if call is None or call.active_agent_id is None:
        return "The caller is being transferred to you."
    agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)
    if agent is None:
        return "The caller is being transferred to you."

    llm_config = LLMConfig(**(agent.config_blob.get("llm") or {}))
    aws_config = AWSConfig(**(agent.config_blob.get("aws") or {}))
    try:
        from turncall.services.credentials import for_project

        result = await complete_text(
            llm_config,
            [
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": f"Transcript:\n{transcript}"},
            ],
            aws=aws_config,
            credentials=await for_project(session, call.project_id),
        )
        return result.text.strip() or "The caller is being transferred to you."
    except Exception:
        logger.exception("transfer_briefing_summary_failed", call_id=str(call_id))
        return "The caller is being transferred to you."


_BRIEFING_LINES = 200


async def briefing_here(body: dict[str, object]) -> dict[str, object]:
    """Another replica's whisper request to this one, which holds the call's
    conversation: the briefing text (`live_transcript.BRIEFING`)."""
    from turncall.services.live_call_channel import ControlRefused
    from turncall.storage.database import get_session_factory

    call_id = UUID(str(body["call_id"]))
    intent = await load_transfer_intent(call_id)
    if intent is None:
        raise ControlRefused("No transfer in flight for this call")
    async with get_session_factory()() as session:
        return {
            "text": await resolve_briefing_text(session, call_id, intent, here=True)
        }


async def _conversation(session: AsyncSession, call_id: UUID) -> list[tuple[str, str]]:
    """(speaker, text) for the call so far, as said (#313): the operator is
    briefed on the caller's own words, which redaction never reaches. The
    stored transcript only when no process holds the call's conversation any
    more (services/live_transcript) — redacted if the project redacts."""
    from turncall.services import live_transcript
    from turncall.storage.repositories import call_repo

    held = live_transcript.lines(call_id)
    if held is not None:
        return [(line.role, line.text) for line in held[:_BRIEFING_LINES]]
    logger.warning(
        "transfer_briefing_from_stored_transcript",
        call_id=str(call_id),
        reason="no process holds the call's conversation",
    )
    events = await call_repo.list_call_events(
        session, call_id, event_type="transcript.final", limit=_BRIEFING_LINES
    )
    conversation = []
    for e in events:
        payload = e.payload or {}
        speaker = payload.get("role") or payload.get("user_id") or ""
        conversation.append((speaker, payload.get("text") or ""))
    return conversation
