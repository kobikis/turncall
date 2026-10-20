"""Grok Voice (the `xai` S2S provider): what its session takes (#387).

A setting on an `xai` agent is either applied or refused, never silently
dropped. Grok's session has no temperature and no output token limit, and the
provider connects to xAI with the xai account's key only — a gateway is S2S
gateway mode on `openai`. Those three are refused.

`s2s.extra` carries the session properties Grok has and S2SConfig does not:
`reasoning` (`{"effort": "high" | "none"}`), `replace` (a spoken word ->
spelling map) and `resumption` (`{"enabled": bool}`), in xAI's own shape. Each
is set onto the session by name. Any other key is warned about and dropped,
never a 422 — the AssemblyAI / Flux `stt.extra` rule — including the session's
other properties (`voice`, `tools`, `turn_detection`), which have homes of
their own.

One rule, applied twice: at the API schema, and at build, since a call-init
inline agent is validated by the domain model rather than that schema (the
`deepgram_flux.mixes_flux` precedent). No pipecat import at module level: the
API schema imports this module.
"""

from typing import Any

from loguru import logger

SESSION_EXTRAS = ("reasoning", "replace", "resumption")

_GATEWAY_MODE = (
    "s2s.base_url is not supported for the xai S2S provider: it connects to "
    "xAI with the xai account's key and nowhere else. To reach Grok Voice "
    "through a gateway, use S2S gateway mode: provider 'openai' with "
    "s2s.base_url"
)
_NO_SUCH_SETTING = (
    "s2s.{field} is not supported for the xai S2S provider: Grok Voice's "
    "session has no {what}; remove s2s.{field}"
)


def refusal(s2s: Any) -> str | None:
    """Why an `xai` S2S config cannot run as written, or None."""
    if s2s.base_url:
        return _GATEWAY_MODE
    if s2s.temperature is not None:
        return _NO_SUCH_SETTING.format(field="temperature", what="temperature")
    if s2s.max_tokens is not None:
        return _NO_SUCH_SETTING.format(field="max_tokens", what="output token limit")
    return _bad_extra(s2s.extra)


def session_extras(extra: dict[str, Any]) -> dict[str, Any]:
    """The `s2s.extra` keys Grok's session takes, as its own field values.

    Assumes `refusal` passed. Every other key is warned about and dropped.
    """
    ignored = sorted(set(extra) - set(SESSION_EXTRAS))
    if ignored:
        logger.warning(
            "xai_s2s_extra_ignored keys={keys}: Grok Voice's session takes only "
            "{known} from s2s.extra",
            keys=ignored,
            known=list(SESSION_EXTRAS),
        )
    known = {key: extra[key] for key in SESSION_EXTRAS if key in extra}
    session = _session_properties().model_validate(known)
    return {key: getattr(session, key) for key in known}


def _bad_extra(extra: dict[str, Any]) -> str | None:
    """A known `s2s.extra` key whose value Grok's session would refuse."""
    from pydantic import ValidationError

    properties = _session_properties()
    for key in SESSION_EXTRAS:
        if key not in extra:
            continue
        try:
            properties.model_validate({key: extra[key]})
        except ValidationError as invalid:
            reason = invalid.errors()[0]["msg"]
            return f"s2s.extra.{key} is not a valid Grok Voice {key}: {reason}"
    return None


def _session_properties() -> Any:
    from pipecat.services.xai.realtime.events import SessionProperties

    return SessionProperties
