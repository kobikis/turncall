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
`deepgram_flux.mixes_flux` precedent). The API schema imports this module and
validates with it, so the refusal runs on TurnCall's own copy of the three
fields (`_SessionExtras`) and never loads pipecat; only `session_extras`, at
build, reads pipecat's model. `test_xai_s2s.py` holds the two to one verdict.
"""

from typing import Any, Literal, Protocol

from loguru import logger
from pydantic import BaseModel, ValidationError

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


class S2SSettings(Protocol):
    """What `refusal` reads: the API's S2S schema and the domain's S2SConfig."""

    @property
    def base_url(self) -> str | None: ...

    @property
    def temperature(self) -> float | None: ...

    @property
    def max_tokens(self) -> int | None: ...

    @property
    def extra(self) -> dict[str, Any]: ...


class _Reasoning(BaseModel):
    effort: Literal["high", "none"] | None = None


class _Resumption(BaseModel):
    enabled: bool | None = None


class _SessionExtras(BaseModel):
    """pipecat's `SessionProperties`, the three fields `s2s.extra` may set."""

    reasoning: _Reasoning | None = None
    replace: dict[str, str] | None = None
    resumption: _Resumption | None = None


def refusal(s2s: S2SSettings) -> str | None:
    """Why an `xai` S2S config cannot run as written, or None."""
    if s2s.base_url:
        return _GATEWAY_MODE
    if s2s.temperature is not None:
        return _NO_SUCH_SETTING.format(field="temperature", what="temperature")
    if s2s.max_tokens is not None:
        return _NO_SUCH_SETTING.format(field="max_tokens", what="output token limit")
    return _bad_extra(s2s.extra)


def session_extras(extra: dict[str, Any]) -> dict[str, Any]:
    """The `s2s.extra` keys Grok's session takes, as pipecat's field values.

    Build time only: it imports pipecat. Assumes `refusal` passed. Every other
    key is warned about and dropped.
    """
    from pipecat.services.xai.realtime.events import SessionProperties

    ignored = sorted(set(extra) - set(SESSION_EXTRAS))
    if ignored:
        logger.warning(
            "xai_s2s_extra_ignored keys={keys}: Grok Voice's session takes only "
            "{known} from s2s.extra",
            keys=ignored,
            known=list(SESSION_EXTRAS),
        )
    known = {key: extra[key] for key in SESSION_EXTRAS if key in extra}
    session = SessionProperties.model_validate(known)
    return {key: getattr(session, key) for key in known}


def _bad_extra(extra: dict[str, Any]) -> str | None:
    """A known `s2s.extra` key whose value Grok's session would refuse."""
    for key in SESSION_EXTRAS:
        if key not in extra:
            continue
        try:
            _SessionExtras.model_validate({key: extra[key]})
        except ValidationError as invalid:
            reason = invalid.errors()[0]["msg"]
            return f"s2s.extra.{key} is not a valid Grok Voice {key}: {reason}"
    return None
