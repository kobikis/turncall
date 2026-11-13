"""Eleven Realtime (the `elevenlabs` S2S provider): what its session takes (#465).

ElevenLabs speaks the OpenAI Realtime protocol at its own endpoint, on the
ElevenLabs account's key — a provider of its own, never S2S gateway mode,
which would send the platform's OpenAI key there. So `s2s.base_url` is refused
here, and a gateway URL on `openai` naming ElevenLabs is refused too.

`s2s.extra` carries what S2SConfig has no field for: `language` (what the
caller is heard in and answered in; ElevenLabs' default is English) and
`eagerness` (semantic VAD's). Any other key is warned about and dropped, the
Grok Voice rule.

A session ends at `max_duration_seconds` (60-7,200, default 600), so it is
sent as the call's own limit plus a margin, and a call allowed longer than a
session can last is refused.

One rule, applied twice: at the API schema, and at build for a call-init
inline agent. No pipecat here; `eleven_realtime_service` is the pipecat half.
"""

from typing import Any
from urllib.parse import urlparse

from loguru import logger

URL = "wss://api.elevenlabs.io/v1/convai/realtime"
MODEL = "eleven_realtime_v1_mini"
VOICE = "brian"
MAX_SESSION_SECONDS = 7200
# The call's own watchdog should end it before ElevenLabs closes the session.
_SESSION_MARGIN_SECONDS = 30
SESSION_EXTRAS = ("language", "eagerness")
_EAGERNESS = ("low", "medium", "high")

_FIXED_ENDPOINT = (
    "s2s.base_url is not supported for the elevenlabs S2S provider: it "
    "connects to ElevenLabs' own endpoint with the elevenlabs account's key"
)
_TO_ELEVENLABS = (
    "s2s.base_url points at ElevenLabs, which would send the platform's OpenAI "
    "key there; use s2s.provider 'elevenlabs' instead"
)


def refusal(s2s: Any) -> str | None:
    """Why an `elevenlabs` S2S config cannot run as written, or None."""
    if s2s.base_url:
        return _FIXED_ENDPOINT
    extra = s2s.extra
    language = extra.get("language")
    if "language" in extra and not (isinstance(language, str) and language.strip()):
        return "s2s.extra.language must be a language code such as 'en' or 'es'"
    if "eagerness" in extra and extra["eagerness"] not in _EAGERNESS:
        return f"s2s.extra.eagerness must be one of {list(_EAGERNESS)}"
    return None


def duration_refusal(max_call_duration_seconds: int) -> str | None:
    if max_call_duration_seconds <= MAX_SESSION_SECONDS:
        return None
    return (
        f"max_call_duration_seconds {max_call_duration_seconds} is longer than "
        f"an Eleven Realtime session can last ({MAX_SESSION_SECONDS} s); "
        f"lower it to {MAX_SESSION_SECONDS} or less"
    )


def gateway_refusal(s2s: Any) -> str | None:
    """S2S gateway mode on `openai` aimed at ElevenLabs."""
    host = urlparse(s2s.base_url or "").hostname or ""
    if s2s.provider == "openai" and (
        host == "elevenlabs.io" or host.endswith(".elevenlabs.io")
    ):
        return _TO_ELEVENLABS
    return None


def session_seconds(max_call_duration_seconds: int) -> int:
    return min(max_call_duration_seconds + _SESSION_MARGIN_SECONDS, MAX_SESSION_SECONDS)


def session_extras(extra: dict[str, Any]) -> dict[str, Any]:
    """The `s2s.extra` keys the session takes; every other key warned about."""
    ignored = sorted(set(extra) - set(SESSION_EXTRAS))
    if ignored:
        logger.warning(
            "elevenlabs_s2s_extra_ignored keys={keys}: Eleven Realtime takes "
            "only {known} from s2s.extra",
            keys=ignored,
            known=list(SESSION_EXTRAS),
        )
    return {key: extra[key] for key in SESSION_EXTRAS if key in extra}
