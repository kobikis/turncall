"""The video Avatar's place in a call's pipeline, and the service it puts there.

HeyGen's LiveAvatar and Tavus are pipecat AIServices that consume the agent's
spoken audio and emit the Avatar's video and their leg's audio (their own
WebRTC leg — LiveKit for HeyGen, Daily for Tavus — is internal). So the stage
sits after whatever speaks: TTS in cascade, the realtime service on
speech-to-speech (#401). Whether a call builds one at all is the build's
decision (`avatar.avatar_gate`); once running it falls back to voice
(`avatar_watch`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from turncall.domain.enums import AvatarUnavailableReason
from turncall.domain.providers import AVATAR_PROVIDERS
from turncall.orchestrator.avatar import AvatarUnavailability
from turncall.orchestrator.observability import _spawn

if TYPE_CHECKING:
    from turncall.domain.models import AgentConfig, AvatarConfig
    from turncall.orchestrator.pipeline_factory import CallContext
    from turncall.services.credentials import Credentials


def create_avatar_service(
    avatar: AvatarConfig,
    credentials: Credentials,
    unavailable: AvatarUnavailability | None = None,
    **watch: float,
) -> Any:
    """The call's Avatar service (HeyGen or Tavus), or None for none.

    Each is watched, and falls back to voice when its provider refuses or
    drops the session (`avatar_watch`), so an Avatar never ends the call; nor
    does one that could not be built at all, which is recorded as refused.
    The build has already checked the provider and the id (`avatar_gate`).
    """
    provider = avatar.provider
    if provider not in AVATAR_PROVIDERS:
        # `avatar_gate` records `unknown_provider` and builds none (#402).
        logger.warning("Unknown avatar provider '{p}'; skipping", p=provider)
        return None
    key = credentials.api_key(provider)
    if not key:
        # The key check fails a call before this (missing_keys); an eval skips it.
        logger.warning("Avatar enabled but no {p} key; skipping", p=provider)
        return None
    build = _heygen if provider == "heygen" else _tavus
    try:
        return build(avatar, key, unavailable=unavailable, **watch)
    except Exception:
        logger.exception("avatar_build_failed", provider=provider)
        if unavailable is not None:
            _spawn(unavailable.record(AvatarUnavailableReason.SESSION_REFUSED))
        return None


def _heygen(avatar: AvatarConfig, key: str, **watched: Any) -> Any:
    # The shared process-wide aiohttp session (closed on app shutdown):
    # pipecat borrows it and never closes it, so a per-call one would leak.
    from pipecat.services.heygen.api_liveavatar import LiveAvatarNewSessionRequest
    from pipecat.services.heygen.client import ServiceType

    from turncall.adapters.aiohttp_client import get_aiohttp_session
    from turncall.orchestrator.avatar_watch import WatchedHeyGenVideoService

    logger.info("Avatar enabled: HeyGen {aid}", aid=avatar.avatar_id)
    return WatchedHeyGenVideoService(
        api_key=key,
        service_type=ServiceType.LIVE_AVATAR,
        session=get_aiohttp_session(),
        session_request=LiveAvatarNewSessionRequest(
            is_sandbox=avatar.is_sandbox,
            avatar_id=avatar.avatar_id,
        ),
        **watched,
    )


def _tavus(avatar: AvatarConfig, key: str, **watched: Any) -> Any:
    from turncall.adapters.aiohttp_client import get_aiohttp_session
    from turncall.orchestrator.avatar_watch import WatchedTavusVideoService

    logger.info("Avatar enabled: Tavus {rid}", rid=avatar.replica_id)
    return WatchedTavusVideoService(
        api_key=key,
        replica_id=avatar.replica_id,
        persona_id=avatar.persona_id,
        session=get_aiohttp_session(),
        **watched,
    )


def avatar_stage(
    config: AgentConfig,
    credentials: Credentials,
    call_context: CallContext,
    *,
    avatar_enabled: bool,
) -> list[Any]:
    """The video Avatar, for directly before `_speech_before_ambience`.

    Gated by avatar_enabled, the build's decision (`avatar_gate`). Ahead of the
    ambience's speech judge, which has to read the Avatar's audio stream,
    silence included (#265).
    """
    if not (avatar_enabled and config.avatar.enabled):
        return []
    avatar = create_avatar_service(
        config.avatar,
        credentials,
        AvatarUnavailability(call_context, config.avatar.provider),
    )
    return [avatar] if avatar else []


def s2s_avatar_stage(
    config: AgentConfig,
    credentials: Credentials,
    call_context: CallContext,
    *,
    avatar_enabled: bool,
) -> list[Any]:
    """`avatar_stage` after a realtime service, which speaks for itself.

    Behind `SpeechForTheAvatar`: gpt-live-1 streams its voice where the others
    hand out TTS audio, the only kind the Avatar reads.
    """
    avatar = avatar_stage(
        config, credentials, call_context, avatar_enabled=avatar_enabled
    )
    if not avatar:
        return []
    from turncall.orchestrator.avatar_speech import SpeechForTheAvatar

    return [SpeechForTheAvatar(), *avatar]
