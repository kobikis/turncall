"""Why a call's Avatar did not show: `avatar.unavailable` (#402).

An Avatar that cannot show never ends the call: the call carries on as voice,
and its event log says why, once per reason — `{provider, reason}`, the reason
one of `AvatarUnavailableReason`. Stored only, never dispatched to webhook
subscribers (like `metrics.turn`), none in an eval.

Two places decide. The build (`avatar_gate`): a call that is not a browser
call, or an Avatar with no id, builds none. Cascade and speech-to-speech alike
(#401): the Avatar reads the agent's spoken audio, which both produce. The
running Avatar service (`orchestrator/avatar_watch.py`): a provider that
refuses the session, or a session that stops mid-call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from turncall.domain.enums import AvatarUnavailableReason, CallEventType
from turncall.orchestrator.turn_metrics import store_call_event

if TYPE_CHECKING:
    from turncall.domain.models import AgentConfig, AvatarConfig
    from turncall.orchestrator.pipeline_factory import CallContext


def missing_id(avatar: AvatarConfig) -> bool:
    """The provider's id for the Avatar is not set."""
    if avatar.provider == "tavus":
        return not avatar.replica_id
    return not avatar.avatar_id


def skip_reason(
    config: AgentConfig, *, carries_video: bool
) -> AvatarUnavailableReason | None:
    """Why an Agent's Avatar cannot be built for this call, or None when it can.

    Only for an Agent whose Avatar is enabled. The transport comes first: a
    phone call could not show the Avatar whatever the Agent said.
    """
    if not carries_video:
        return AvatarUnavailableReason.TRANSPORT
    if missing_id(config.avatar):
        return AvatarUnavailableReason.MISSING_ID
    return None


class AvatarUnavailability:
    """A call's `avatar.unavailable` records: at most one per reason."""

    def __init__(self, call_context: CallContext, provider: str) -> None:
        self._call_context = call_context
        self._provider = provider
        self._recorded: set[AvatarUnavailableReason] = set()

    async def record(self, reason: AvatarUnavailableReason) -> None:
        if reason in self._recorded:
            return
        self._recorded.add(reason)
        logger.warning(
            "avatar_unavailable",
            call_id=str(self._call_context.call_id),
            provider=self._provider,
            reason=str(reason),
        )
        if not self._call_context.records:
            return
        await store_call_event(
            self._call_context,
            CallEventType.AVATAR_UNAVAILABLE,
            {"provider": self._provider, "reason": str(reason)},
        )


async def avatar_gate(
    config: AgentConfig, call_context: CallContext, *, carries_video: bool
) -> bool:
    """Whether to build the call's Avatar; when not, the reason is recorded."""
    if not config.avatar.enabled:
        return False
    reason = skip_reason(config, carries_video=carries_video)
    if reason is None:
        return True
    await AvatarUnavailability(call_context, config.avatar.provider).record(reason)
    return False
