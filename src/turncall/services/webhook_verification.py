"""Carrier webhooks nothing here can verify (#337, #339).

One rule says where a Twilio or WhatsApp webhook that no token or secret can
verify is taken: on a development deployment only. Staging and production
refuse it with a 403. `TURNCALL_ENV` defaults to development, so a deployment
that never set it accepts them, and startup says so in plain words.

Twilio's predicate is `twilio_accounts.unverifiable_webhooks_refused`, since
whether a Twilio webhook can be verified also depends on the numbers' own
accounts; WhatsApp's is `whatsapp_unverifiable_refused` here. Each is used by
its handler and by its startup warning, so the two cannot disagree.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from turncall.config.settings import Environment
from turncall.storage.repositories import phone_number_repo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.config.settings import Settings


def accepts_unverified(settings: Settings) -> bool:
    """Whether this environment takes a webhook nothing can verify: only
    development does."""
    return settings.environment == Environment.DEVELOPMENT


def outcome(settings: Settings, *, refused: bool) -> str:
    """What happens to an unverifiable webhook, for a startup warning. When it
    is accepted because TURNCALL_ENV was never set, it says so."""
    if refused:
        return "refused (403)"
    if settings.environment_is_default:
        return (
            "accepted unverified, because TURNCALL_ENV is not set and defaults "
            "to development; set TURNCALL_ENV=production (or staging) on any "
            "deployment that is not a development one, and they are refused"
        )
    return "accepted unverified (TURNCALL_ENV=development)"


def whatsapp_unverifiable_refused(settings: Settings) -> bool:
    """Whether a WhatsApp webhook WHATSAPP_APP_SECRET cannot verify is refused
    (#339): everywhere but development."""
    return not accepts_unverified(settings)


async def warn_if_whatsapp_unverifiable(
    session: AsyncSession, settings: Settings
) -> str | None:
    """At startup, warn when WhatsApp is configured but WHATSAPP_APP_SECRET is
    not (#339).

    WhatsApp counts as configured when any other WHATSAPP_* setting is set
    (the access token, phone number id or webhook verify token) or a phone
    number is bound with `whatsapp_enabled`. Returns the warning, or None.
    """
    whatsapp = settings.whatsapp
    if whatsapp.app_secret:
        return None
    configured = [
        name
        for name, value in (
            ("WHATSAPP_TOKEN", whatsapp.token),
            ("WHATSAPP_PHONE_NUMBER_ID", whatsapp.phone_number_id),
            ("WHATSAPP_WEBHOOK_VERIFY_TOKEN", whatsapp.webhook_verify_token),
        )
        if value
    ]
    if await phone_number_repo.any_whatsapp_enabled(session):
        configured.append("a whatsapp_enabled phone number")
    if not configured:
        return None
    refused = whatsapp_unverifiable_refused(settings)
    message = (
        f"whatsapp_app_secret_missing | WhatsApp is configured "
        f"({', '.join(configured)}) but WHATSAPP_APP_SECRET is not set: "
        f"WhatsApp webhooks are {outcome(settings, refused=refused)}"
    )
    logger.warning(message)
    return message


def bounded(value: object, limit: int = 64) -> str:
    """A request-supplied value fit for a log line: printable characters only,
    at most `limit` of them."""
    text = "".join(c for c in str(value) if c.isprintable())
    return text if len(text) <= limit else f"{text[:limit]}..."


class RefusalLog:
    """A refused webhook said at most once per `interval` seconds per route.

    Anyone who can reach a webhook can be refused as often as they like, and
    the configuration behind the refusal is already reported at startup. So a
    route's first refusal is a warning, the ones in the next `interval`
    seconds are only counted, and the next warning says how many it left out.
    Keyed by route template, so the keys are bounded.
    """

    def __init__(
        self, interval: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def warn(self, route: str, message: str, **fields: object) -> bool:
        """Log `message` (formatted with `fields`, which are bounded first)
        unless this route logged one within the interval. Returns whether it
        logged."""
        now = self._clock()
        last = self._last.get(route)
        if last is not None and now - last < self._interval:
            self._suppressed[route] = self._suppressed.get(route, 0) + 1
            return False
        self._last[route] = now
        suppressed = self._suppressed.pop(route, 0)
        if suppressed:
            message += " ({suppressed} more refused on this route since the last)"
        logger.warning(
            message,
            route=route,
            suppressed=suppressed,
            **{name: bounded(value) for name, value in fields.items()},
        )
        return True

    def reset(self) -> None:
        self._last.clear()
        self._suppressed.clear()


# One for the process: Twilio's and WhatsApp's routes are distinct keys.
refused_webhooks = RefusalLog()
