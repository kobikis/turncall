"""How a carrier refusing, or not answering, reads to an API client: the
mapping the phone-number routes share (bind in `phone_numbers.py`; search,
buy and release in `phone_number_purchase.py`).

- **The caller's to fix (422):** credentials the caller supplied that the
  carrier rejects, and, where the request is the caller's own (a number to
  buy or release, a search filter), a request the carrier refuses.
- **The carrier's (502):** anything else it refuses, its failures, and a
  request it never answered (`twilio_error` / `telnyx_error`).

Bind's Telnyx lookup is TurnCall's own request (a listing by E.164), so a
4xx refusing it is the carrier's, not the caller's: bind passes
`request_is_callers=False`.
"""

from __future__ import annotations

from typing import Any, NoReturn

from turncall.api.errors import ApiError, ErrorCode


def unprocessable(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR,
        message=message,
        details=details,
    )


def twilio_failed(exc: Any, *, own: bool) -> NoReturn:
    """Twilio refused (a `TwilioRestException`): the caller's own credentials
    are theirs to fix (422); the platform's, or Twilio failing, are not
    (502)."""
    if own and exc.status in (401, 403):
        raise unprocessable(
            "Twilio rejected this Account SID / Auth Token pair"
        ) from exc
    raise ApiError(
        status_code=502, code=ErrorCode.TWILIO_ERROR, message=f"Twilio: {exc.msg}"
    ) from exc


def telnyx_failed(exc: Any, *, request_is_callers: bool) -> NoReturn:
    """Telnyx refused (a `TelnyxApiError`). The account's key is the
    operator's to fix (422), the project's and the platform's alike. A 400,
    404 or 422 is the caller's (422, with Telnyx's codes) when the request is
    theirs (a number no longer for sale, a filter Telnyx rejects), else
    Telnyx's (502), as is every other failure."""
    if exc.status in (401, 403):
        raise unprocessable("Telnyx rejected this account's API key") from exc
    if request_is_callers and exc.status in (400, 404, 422):
        raise unprocessable(str(exc), details={"telnyx_codes": exc.codes}) from exc
    raise ApiError(
        status_code=502, code=ErrorCode.TELNYX_ERROR, message=str(exc)
    ) from exc


def telnyx_unanswered(exc: Exception, consequence: str = "") -> NoReturn:
    """Telnyx did not answer (an `httpx.HTTPError`: a timeout, a dropped
    connection): 502. `consequence` says what the client must check, when
    the request may have taken effect anyway."""
    message = f"Telnyx did not answer ({type(exc).__name__})"
    raise ApiError(
        status_code=502,
        code=ErrorCode.TELNYX_ERROR,
        message=f"{message}: {consequence}" if consequence else message,
    ) from exc
