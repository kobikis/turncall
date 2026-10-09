"""Telnyx telephony adapter: Call Control v2 commands (ADR-0022).

Every Telnyx REST request TurnCall makes goes through here, built by
`services/telnyx_accounts.TelnyxAccount.adapter` — the one constructor, which
is what a test's fake carrier replaces. A command addresses a call by its
`call_control_id` (stored as the call's `provider_call_sid`).

A transfer is not one command on Telnyx: it is a sequence (stop the agent's
stream, speak, ring the operator on a second leg, whisper, bridge) driven by
the webhooks each step produces, which `services/telnyx_transfer.py` runs. The
steps are this adapter's.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote, urlsplit

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
# Telnyx's own TTS, as Twilio's <Say> is Twilio's (ADR-0009: the carrier voices
# the transfer message, the briefing and the fallback, not the agent's TTS).
SPEAK_VOICE = "AWS.Polly.Joanna"
SPEAK_MAX_CHARS = 3000  # Telnyx refuses a longer `payload`
# Every Call Control application TurnCall creates is named this, then a host.
APPLICATION_PREFIX = "TurnCall "
# The webhook format `webhooks/telnyx_handlers.py` reads ({"data": {...}}).
# Telnyx's default for a new application is "1", a different shape.
_WEBHOOK_API_VERSION = "2"


class TelnyxApiError(RuntimeError):
    """Telnyx refused a command. `status` is the HTTP status, `codes` its
    error codes (strings, as Telnyx sends them)."""

    def __init__(self, status: int, codes: list[str], detail: str) -> None:
        self.status = status
        self.codes = codes
        super().__init__(f"Telnyx {status}: {detail}")


class TelnyxNumberError(RuntimeError):
    """A number this account cannot use the way TurnCall needs: not on the
    account, or on no Call Control application to dial from."""


def _media_stream(stream_url: str) -> dict[str, str]:
    """The fields that point a call's media at `stream_url`, on `answer` and
    `dial` alike. Only the far end's track is streamed; the agent's audio goes
    back over the same socket as RTP in `STREAM_CODEC`."""
    return {
        "stream_url": stream_url,
        "stream_track": "inbound_track",
        "stream_codec": STREAM_CODEC,
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": STREAM_CODEC,
    }


def application_name(webhook_url: str) -> str:
    """The Call Control application TurnCall keeps for one deployment: named by
    the host its webhooks go to, so two deployments sharing a Telnyx account
    (staging and production) each keep their own and never repoint the
    other's numbers."""
    return f"{APPLICATION_PREFIX}{urlsplit(webhook_url).netloc}"


class TelnyxAdapter(TelephonyAdapter):
    """One Telnyx account's Call Control commands."""

    def __init__(
        self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._api_key = api_key
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One REST request; the response's JSON body. Raises `TelnyxApiError`
        on anything but success."""
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT_SECONDS
        ) as client:
            response = await client.request(
                method,
                f"{TELNYX_API_BASE}{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        if response.is_success:
            return dict(response.json()) if response.content else {}
        try:
            errors = response.json().get("errors") or []
        except ValueError:
            errors = []
        codes = [str(e.get("code", "")) for e in errors]
        detail = "; ".join(str(e.get("detail") or e.get("title")) for e in errors)
        raise TelnyxApiError(response.status_code, codes, detail or response.text)

    async def _command(
        self, call_control_id: str, action: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST /calls/{id}/actions/{action}; the response's `data`."""
        path = f"/calls/{quote(call_control_id, safe=':')}/actions/{action}"
        return dict(
            (await self._request("POST", path, body=body or {})).get("data") or {}
        )

    async def answer_call(self, call_control_id: str, *, stream_url: str) -> None:
        """Answer a ringing call with a bidirectional media stream to `stream_url`.

        Telnyx's way of answering (a Call Control command, where Twilio reads
        TwiML), so it is this adapter's own rather than the interface's.
        Only the caller's track is streamed; the agent's audio goes back over
        the same socket as RTP in `STREAM_CODEC`.
        """
        await self._command(call_control_id, "answer", _media_stream(stream_url))
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
        connection_id: str = "",
        stream_url: str = "",
    ) -> str:
        """Dial `to_number` from `from_number`; the new call's `call_control_id`.

        A Telnyx dial runs on a Call Control application: `connection_id`, or
        else the caller-id number's own. Every event of the call goes to
        `webhook_url` (Call Control has no separate status callback, so
        `status_callback_url` is unused), and once the callee answers its media
        goes to `stream_url`: there is no answer step on an outbound call.
        """
        body = {
            "to": to_number,
            "from": from_number,
            "connection_id": connection_id or await self.connection_for(from_number),
            "webhook_url": webhook_url,
            **(_media_stream(stream_url) if stream_url else {}),
        }
        data = (await self._request("POST", "/calls", body=body)).get("data") or {}
        call_control_id = str(data["call_control_id"])
        logger.info("telnyx_call_dialled", call_control_id=call_control_id)
        return call_control_id

    async def connection_for(self, e164: str) -> str:
        """The Call Control application the number `e164` is on — what a dial
        from it runs on. Raises `TelnyxNumberError` naming why there is none."""
        number = await self.find_number(e164)
        if number is None:
            raise TelnyxNumberError(f"{e164} is not on this Telnyx account")
        if not number.get("connection_id"):
            raise TelnyxNumberError(
                f"{e164} is on no Call Control application to dial from; "
                "update its binding (PUT /v1/phone-numbers/{id}) to route it"
            )
        return str(number["connection_id"])

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
        raise NotImplementedError(
            "A Telnyx transfer is a sequence of commands driven by its webhooks: "
            "services/telnyx_transfer.start"
        )

    async def send_dtmf(self, provider_call_sid: str, digits: str) -> None:
        """Play DTMF on the call (0-9, A-D, *, #; w/W pause half/one second)."""
        await self._command(provider_call_sid, "send_dtmf", {"digits": digits})
        logger.info("telnyx_dtmf_sent", call_control_id=provider_call_sid)

    # -- transfer steps (services/telnyx_transfer.py) ----------------------

    async def stop_streaming(self, call_control_id: str, *, client_state: str) -> None:
        """Stop the call's media stream. The call stays up; the agent leaves it."""
        await self._command(
            call_control_id, "streaming_stop", {"client_state": client_state}
        )

    async def speak(
        self, call_control_id: str, text: str, *, client_state: str
    ) -> None:
        """Say `text` on this leg in Telnyx's voice. `call.speak.ended` follows,
        carrying `client_state`."""
        await self._command(
            call_control_id,
            "speak",
            {
                "payload": text[:SPEAK_MAX_CHARS],
                "voice": SPEAK_VOICE,
                "client_state": client_state,
            },
        )

    async def start_playback(self, call_control_id: str, wav: bytes) -> None:
        """Play a WAV on this leg, looped until stopped."""
        await self._command(
            call_control_id,
            "playback_start",
            {
                "playback_content": base64.b64encode(wav).decode(),
                "audio_type": "wav",
                "loop": "infinity",
            },
        )

    async def stop_playback(self, call_control_id: str) -> None:
        await self._command(call_control_id, "playback_stop", {"stop": "all"})

    async def dial(
        self,
        *,
        connection_id: str,
        to: str,
        from_number: str,
        link_to: str,
        client_state: str,
        detect_machine: bool,
    ) -> str:
        """Ring `to` on a new leg of `link_to`'s call; its `call_control_id`.

        `bridge_intent` presents the linked call's caller ID to `to`, as
        Twilio's `<Dial>` does. Machine detection only reports — the leg is
        bridged by whoever handles `call.answered`, never held for it.
        """
        response = await self._request(
            "POST",
            "/calls",
            body={
                "connection_id": connection_id,
                "to": to,
                "from": from_number,
                "link_to": link_to,
                "bridge_intent": True,
                "client_state": client_state,
                **({"answering_machine_detection": "detect"} if detect_machine else {}),
            },
        )
        return str((response.get("data") or {})["call_control_id"])

    async def bridge(
        self, call_control_id: str, other: str, *, client_state: str
    ) -> None:
        """Connect this leg's audio to `other`'s."""
        await self._command(
            call_control_id,
            "bridge",
            {"call_control_id": other, "client_state": client_state},
        )

    async def get_recording_url(self, recording_sid: str) -> str:
        # Recording is app-side (ADR-0005); Telnyx records nothing for us.
        raise NotImplementedError("TurnCall records Telnyx calls itself (ADR-0005)")

    async def configure_number_webhook(
        self, number_sid: str, voice_url: str, status_callback_url: str
    ) -> None:
        """Route the number (Telnyx's phone number id) to `voice_url`, or away
        from TurnCall when it is "".

        Telnyx routes a number by the Call Control application it is assigned
        to, so routing it is assigning it to this deployment's application
        (created on first use, its webhook URL corrected on every bind), and
        unrouting is unassigning it. Call Control posts every event to that one
        URL; `status_callback_url` is unused. An application of TurnCall's left
        with no numbers — the previous host's after PUBLIC_BASE_URL changed, or
        this one after the last unbind — is deleted, so the account does not
        fill with them. A number now on someone else's connection is left
        there: unbinding it is not ours to undo.
        """
        path = f"/phone_numbers/{quote(number_sid, safe='')}"
        previous = (await self.fetch_number_info(number_sid)).get("connection_id")
        target = await self.ensure_application(voice_url) if voice_url else None
        if target is None and not await self._is_ours(previous):
            return
        if previous != target:
            await self._request("PATCH", path, body={"connection_id": target})
            logger.info(
                "telnyx_number_routed",
                number_id=number_sid,
                connection_id=target,
                previous=previous,
            )
        if previous and previous != target:
            await self.delete_application_if_unused(previous)

    async def fetch_number_info(self, number_sid: str) -> dict[str, Any]:
        path = f"/phone_numbers/{quote(number_sid, safe='')}"
        return dict((await self._request("GET", path)).get("data") or {})

    async def find_number(self, e164: str) -> dict[str, Any] | None:
        """The account's phone number `e164`, or None when it cannot see it.

        Telnyx's filter takes digits only (a `+` matches nothing) and matches
        partially, so the exact number is picked out of what comes back.
        """
        found = await self._request(
            "GET",
            "/phone_numbers",
            params={"filter[phone_number]": e164.lstrip("+"), "page[size]": 250},
        )
        return next(
            (dict(n) for n in found.get("data") or [] if n.get("phone_number") == e164),
            None,
        )

    async def ensure_application(self, webhook_url: str) -> str:
        """This deployment's Call Control application, pointed at
        `webhook_url`, created when missing; its id."""
        name = application_name(webhook_url)
        found = await self._request(
            "GET",
            "/call_control_applications",
            params={"filter[application_name][contains]": name, "page[size]": 250},
        )
        settings = {
            "webhook_event_url": webhook_url,
            "webhook_api_version": _WEBHOOK_API_VERSION,
        }
        for app in found.get("data") or []:
            if app.get("application_name") == name:
                if any(app.get(k) != v for k, v in settings.items()):
                    await self._request(
                        "PATCH",
                        f"/call_control_applications/{app['id']}",
                        body=settings,
                    )
                return str(app["id"])
        created = await self._request(
            "POST",
            "/call_control_applications",
            body={"application_name": name, **settings, **await self._outbound()},
        )
        app_id = str(created["data"]["id"])
        logger.info("telnyx_application_created", application_id=app_id, name=name)
        return app_id

    async def _outbound(self) -> dict[str, Any]:
        """The outbound voice profile a new application dials through. Telnyx
        refuses a dial on an application with none, and an account usually has
        exactly one (its "Default"); with several, which one is the operator's
        call, so none is chosen and outbound waits on the console."""
        profiles = (await self._request("GET", "/outbound_voice_profiles")).get(
            "data"
        ) or []
        if len(profiles) != 1:
            logger.warning(
                "telnyx_outbound_voice_profile_not_chosen", count=len(profiles)
            )
            return {}
        return {"outbound": {"outbound_voice_profile_id": str(profiles[0]["id"])}}

    async def _application(self, connection_id: str | None) -> dict[str, Any] | None:
        """The Call Control application `connection_id`, or None when it is not
        one (another kind of connection, or gone)."""
        if not connection_id:
            return None
        try:
            found = await self._request(
                "GET", f"/call_control_applications/{quote(connection_id, safe='')}"
            )
        except TelnyxApiError as exc:
            if exc.status == 404:
                return None
            raise
        return dict(found.get("data") or {})

    async def _is_ours(self, connection_id: str | None) -> bool:
        app = await self._application(connection_id)
        return bool(app) and str(app.get("application_name", "")).startswith(
            APPLICATION_PREFIX
        )

    async def delete_application_if_unused(self, connection_id: str) -> bool:
        """Delete one of TurnCall's applications once no number is on it.
        Whether it was deleted; anyone else's is never touched."""
        if not await self._is_ours(connection_id):
            return False
        on_it = await self._request(
            "GET",
            "/phone_numbers",
            params={"filter[connection_id]": connection_id, "page[size]": 1},
        )
        if on_it.get("data"):
            return False
        await self._request(
            "DELETE", f"/call_control_applications/{quote(connection_id, safe='')}"
        )
        logger.info("telnyx_application_deleted", application_id=connection_id)
        return True

    async def send_sms(self, from_number: str, to_number: str, body: str) -> str:
        raise NotImplementedError("SMS on Telnyx numbers is not supported")

    async def configure_sms_webhook(self, number_sid: str, sms_url: str) -> None:
        raise NotImplementedError("SMS on Telnyx numbers is not supported")
