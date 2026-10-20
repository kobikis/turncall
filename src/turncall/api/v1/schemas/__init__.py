"""V1 API request/response schemas."""

from __future__ import annotations

from pydantic import ValidationInfo

# Every phone number the API accepts; one pattern, so a suppressed number and a
# dialled one compare as the same string (#227).
E164_PATTERN = r"^\+[1-9]\d{1,14}$"

# The validation context for an inline agent config: an eval run's `inline`
# target, or a `turncall-demo` agents file. Neither is refused for a plain
# http:// URL (#340), as a call-init answer's inline agent is not; a delivery
# to one is logged instead (services/outbound_url.py).
INLINE_CONFIG = {"inline_config": True}


def outbound_url(value: str | None, info: ValidationInfo, label: str) -> str | None:
    """A field validator's check of an outbound URL sent call data: https://
    outside development (#340), and http(s) with a host everywhere. None or
    empty is left to the field. Imported here: schemas load before services."""
    if not value:
        return value
    from turncall.services.outbound_url import check_outbound_url

    inline = bool(info.context and info.context.get("inline_config"))
    return check_outbound_url(value, label=label, allow_plain_http=inline)
