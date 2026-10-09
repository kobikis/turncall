"""Telnyx telephony adapter: Call Control v2 commands (ADR-0022).

Every Telnyx REST request TurnCall makes goes through here, built by
`services/telnyx_accounts.TelnyxAccount.adapter` — the one constructor, which
is what a test's fake carrier replaces. A command addresses a call by its
`call_control_id` (stored as the call's `provider_call_sid`).

The actions later tickets bring (outbound dial #218, number configuration
#219, transfer and DTMF #220) land here as well; until then they raise, naming
the ticket, rather than pretend to have done something.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from turncall.adapters.telephony.base import TelephonyAdapter

TELNYX_API_BASE = "https://api.telnyx.com/v2"
# What the media stream carries both ways: 8 kHz mu-law, the encoding pipecat's
# TelnyxFrameSerializer and the 8 kHz rendition of an ambience sound expect.
STREAM_CODEC = "PCMU"
# Telnyx's answer to a command on a call that has already hung up.
CALL_ALREADY_ENDED = "90018"
_TIMEOUT_SECONDS = 10.0


class TelnyxApiError(RuntimeError):
    """Telnyx refused a command. `status` is the HTTP status, `codes` its
    error codes (strings, as Telnyx sends them)."""

    def __init__(self, status: int, codes: list[str], detail: str) -> None:
        self.status = status
        self.codes = codes
        super().__init__(f"Telnyx {status}: {detail}")


def _not_yet(action: str, ticket: str) -> NotImplementedError:
    return NotImplementedError(f"{action} on a Telnyx number arrives with {ticket}")


class TelnyxAdapter(TelephonyAdapter):
    """One Telnyx account's Call Control commands."""

    def __init__(
        self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._api_key = api_key
        self._transport = transport

    async def _command(
        self, call_control_id: str, action: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST /calls/{id}/actions/{action}; the response's `data`."""
        url = f"{TELNYX_API_BASE}/calls/{quote(call_control_id, safe=':')}/actions/{action}"
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT_SECONDS
        ) as client:
            response = await client.post(
                url,
                json=body or {},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        if response.is_success:
            return dict(response.json().get("data") or {})
        try:
            errors = response.json().get("errors") or []
        except ValueError:
            errors = []
        codes = [str(e.get("code", "")) for e in errors]
        detail = "; ".join(str(e.get("detail") or e.get("title")) for e in errors)
        raise TelnyxApiError(response.status_code, codes, detail or response.text)

    async def answer_call(self, call_control_id: str, *, stream_url: str) -> None:
        """Answer a ringing call with a bidirectional media stream to `stream_url`.

        Telnyx's way of answering (a Call Control command, where Twilio reads
        TwiML), so it is this adapter's own rather than the interface's.
        Only the caller's track is streamed; the agent's audio goes back over
        the same socket as RTP in `STREAM_CODEC`.
        """
        await self._command(
            call_control_id,
            "answer",
            {
                "stream_url": stream_url,
                "stream_track": "inbound_track",
                "stream_codec": STREAM_CODEC,
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": STREAM_CODEC,
            },
        )
        logger.info("telnyx_call_answered", call_control_id=call_control_id)

    async def reject_call(
        self, call_control_id: str, *, cause: str = "USER_BUSY"
    ) -> None:
        """Refuse a ringing call without answering it — the caller hears busy
        (`USER_BUSY`) or a rejection (`CALL_REJECTED`). Telnyx-only, like
        `answer_call`: Twilio refuses with TwiML."""
        await self._command(call_control_id, "reject", {"cause": cause})
        logger.info(
            "telnyx_call_rejected", call_control_id=call_control_id, cause=cause
        )

    async def end_call(self, provider_call_sid: str) -> None:
        """Hang up. A call that has already ended counts as hung up."""
        try:
            await self._command(provider_call_sid, "hangup")
        except TelnyxApiError as exc:
            if CALL_ALREADY_ENDED in exc.codes:
                return
            raise
        logger.info("telnyx_call_ended", call_control_id=provider_call_sid)

    async def initiate_outbound_call(
        self,
        from_number: str,
        to_number: str,
        webhook_url: str,
        *,
        status_callback_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        raise _not_yet("An outbound call", "#218")

    async def transfer_call(
        self,
        provider_call_sid: str,
        target_number: str,
        *,
        transfer_message: str | None = None,
        whisper_url: str | None = None,
        action_url: str | None = None,
        amd_callback_url: str | None = None,
    ) -> str:
        raise _not_yet("A transfer", "#220")

    async def send_dtmf(self, provider_call_sid: str, digits: str) -> None:
        raise _not_yet("Sending DTMF", "#220")

    async def get_recording_url(self, recording_sid: str) -> str:
        # Recording is app-side (ADR-0005); Telnyx records nothing for us.
        raise NotImplementedError("TurnCall records Telnyx calls itself (ADR-0005)")

    async def configure_number_webhook(
        self, number_sid: str, voice_url: str, status_callback_url: str
    ) -> None:
        raise _not_yet("Configuring a number", "#219")

    async def fetch_number_info(self, number_sid: str) -> dict[str, Any]:
        raise _not_yet("Looking up a number", "#219")

    async def send_sms(self, from_number: str, to_number: str, body: str) -> str:
        raise NotImplementedError("SMS on Telnyx numbers is not supported")

    async def configure_sms_webhook(self, number_sid: str, sms_url: str) -> None:
        raise NotImplementedError("SMS on Telnyx numbers is not supported")
