"""Twilio telephony adapter implementation."""

import asyncio
import json
import xml.etree.ElementTree as ET
from typing import Any

from loguru import logger
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from turncall.adapters.telephony.base import TelephonyAdapter
from turncall.domain.carriers import NUMBER_TYPES

# How many partial matches each page of a number lookup reads.
_LOOKUP_PAGE_SIZE = 50


class TwilioAdapter(TelephonyAdapter):
    """Twilio telephony adapter."""

    def __init__(self, account_sid: str, auth_token: str) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._client = TwilioClient(account_sid, auth_token)
        self._validator = RequestValidator(auth_token)

    async def initiate_outbound_call(
        self,
        from_number: str,
        to_number: str,
        webhook_url: str,
        *,
        status_callback_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Initiate an outbound call via Twilio. Returns Call SID."""
        kwargs: dict[str, Any] = {
            "to": to_number,
            "from_": from_number,
            "url": webhook_url,
        }
        if status_callback_url:
            kwargs["status_callback"] = status_callback_url
            kwargs["status_callback_event"] = [
                "initiated",
                "ringing",
                "answered",
                "completed",
            ]

        call = self._client.calls.create(**kwargs)
        logger.info(
            "twilio_outbound_call_created",
            call_sid=call.sid,
            to=to_number,
            from_=from_number,
        )
        return call.sid

    async def end_call(self, provider_call_sid: str) -> None:
        """Hang up an active call."""
        self._client.calls(provider_call_sid).update(status="completed")
        logger.info("twilio_call_ended", call_sid=provider_call_sid)

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
        """Transfer a call by swapping in <Dial> TwiML. Returns the call SID."""
        twiml = self.build_transfer_twiml(
            target_number,
            transfer_message=transfer_message,
            whisper_url=whisper_url,
            action_url=action_url,
            amd_callback_url=amd_callback_url,
        )
        self._client.calls(provider_call_sid).update(twiml=twiml)
        logger.info(
            "twilio_call_transfer_initiated",
            call_sid=provider_call_sid,
            target=target_number,
            warm=whisper_url is not None,
        )
        return provider_call_sid

    async def send_dtmf(self, provider_call_sid: str, digits: str) -> None:
        """Send DTMF tones."""
        twiml = f'<Response><Play digits="{digits}"/></Response>'
        self._client.calls(provider_call_sid).update(twiml=twiml)
        logger.info(
            "twilio_dtmf_sent",
            call_sid=provider_call_sid,
            digits=digits,
        )

    def validate_webhook_signature(
        self,
        signature: str,
        url: str,
        params: dict[str, str],
    ) -> bool:
        """Validate a Twilio webhook signature. Not a `TelephonyAdapter`
        method: a signature check is the inbound edge's (ADR-0022)."""
        return self._validator.validate(url, params, signature)

    async def get_recording_url(self, recording_sid: str) -> str:
        """Get the URL for a call recording."""
        recording = self._client.recordings(recording_sid).fetch()
        return f"https://api.twilio.com{recording.uri.replace('.json', '.mp3')}"

    async def delete_recording(self, recording_sid: str) -> None:
        """Delete a recording from the account (retention, #316). One that is
        already gone counts as deleted. Off the event loop: the SDK blocks."""

        def _delete() -> None:
            try:
                self._client.recordings(recording_sid).delete()
            except TwilioRestException as exc:
                if exc.status != 404:
                    raise

        await asyncio.to_thread(_delete)

    async def configure_number_webhook(
        self,
        number_sid: str,
        voice_url: str,
        status_callback_url: str,
    ) -> None:
        """Configure webhook URLs on a Twilio phone number."""
        self._client.incoming_phone_numbers(number_sid).update(
            voice_url=voice_url,
            voice_method="POST",
            status_callback=status_callback_url,
            status_callback_method="POST",
        )
        logger.info(
            "twilio_number_webhook_configured",
            number_sid=number_sid,
            voice_url=voice_url,
        )

    async def fetch_number_info(self, number_sid: str) -> dict[str, Any]:
        """Fetch phone number details from Twilio."""
        number = self._client.incoming_phone_numbers(number_sid).fetch()
        return {
            "sid": number.sid,
            "phone_number": number.phone_number,
            "friendly_name": number.friendly_name,
            "voice_url": number.voice_url,
            "status_callback": number.status_callback,
        }

    async def find_number_sid(self, e164_number: str) -> str | None:
        """The PN SID of this E.164 number on this account, or None if the
        account does not own it. Raises TwilioRestException on bad credentials
        (a refused list arrives from the SDK as a bare TwilioException; it is
        converted, so a 401 reads as one). Off the event loop.

        Twilio's PhoneNumber filter matches partially (a number that merely
        contains `e164_number` matches too), so every match is read, page by
        page, until the exact number turns up: a release or a bind must never
        act on a different number.
        """

        def _find() -> str | None:
            listing = self._client.incoming_phone_numbers
            try:
                for number in listing.stream(
                    phone_number=e164_number, page_size=_LOOKUP_PAGE_SIZE
                ):
                    if number.phone_number == e164_number:
                        return str(number.sid)
            except TwilioRestException:
                raise
            except TwilioException as exc:
                raise _page_error(exc, listing._uri) from exc
            return None

        return await asyncio.to_thread(_find)

    async def search_numbers(
        self,
        country: str,
        *,
        number_type: str = "local",
        area_code: str | None = None,
        contains: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Numbers this account can buy, voice-capable, in TurnCall's shape.

        Twilio's own (ADR-0025), not a `TelephonyAdapter` action. Raises
        TwilioRestException: 404 when Twilio sells no `number_type` numbers in
        `country`, 401/403 on bad credentials. Off the event loop: the SDK
        blocks.
        """
        if number_type not in NUMBER_TYPES:
            raise ValueError(f"Twilio has no number type {number_type!r}")
        filters: dict[str, Any] = {"voice_enabled": True, "limit": limit}
        if area_code:
            # As given: a leading zero is part of an area code.
            filters["area_code"] = area_code
        if contains:
            filters["contains"] = contains

        def _search() -> list[Any]:
            numbers = self._client.available_phone_numbers(country)
            # The SDK names each type's list as TurnCall does.
            listing = getattr(numbers, number_type)
            try:
                return list(listing.list(**filters))
            except TwilioRestException:
                raise
            except TwilioException as exc:
                raise _page_error(exc, listing._uri) from exc

        records = await asyncio.to_thread(_search)
        return [_available_number(record, number_type) for record in records]

    async def buy_number(self, e164_number: str) -> dict[str, Any]:
        """Buy the number onto this account; what binding it needs.

        Raises TwilioRestException: 400 when the number is no longer available
        or its country needs an address or regulatory bundle first, 401/403 on
        bad credentials. Off the event loop.
        """

        def _buy() -> Any:
            return self._client.incoming_phone_numbers.create(phone_number=e164_number)

        number = await asyncio.to_thread(_buy)
        logger.info(
            "twilio_number_bought", number_sid=number.sid, e164=number.phone_number
        )
        return {
            "number_sid": number.sid,
            "e164_number": number.phone_number,
            "friendly_name": number.friendly_name,
            # A Twilio purchase is immediate: the number can be bound now.
            "status": "active",
        }

    async def release_number(self, number_sid: str) -> None:
        """Release the number from this account, back to Twilio. Irreversible:
        Twilio may sell it to someone else.

        Twilio's own (ADR-0025), not a `TelephonyAdapter` action. Raises
        TwilioRestException: 404 when the account does not own `number_sid`,
        401/403 on bad credentials. Off the event loop.
        """

        def _release() -> None:
            self._client.incoming_phone_numbers(number_sid).delete()

        await asyncio.to_thread(_release)
        logger.info("twilio_number_released", number_sid=number_sid)

    async def send_sms(
        self,
        from_number: str,
        to_number: str,
        body: str,
    ) -> str:
        """Send an SMS message. Returns provider message SID."""
        message = self._client.messages.create(
            to=to_number,
            from_=from_number,
            body=body,
        )
        logger.info(
            "twilio_sms_sent",
            message_sid=message.sid,
            to=to_number,
            from_=from_number,
        )
        return message.sid

    async def configure_sms_webhook(
        self,
        number_sid: str,
        sms_url: str,
    ) -> None:
        """Configure the SMS webhook URL on a Twilio phone number."""
        self._client.incoming_phone_numbers(number_sid).update(
            sms_url=sms_url,
            sms_method="POST",
        )
        logger.info(
            "twilio_sms_webhook_configured",
            number_sid=number_sid,
            sms_url=sms_url,
        )

    @staticmethod
    def build_transfer_twiml(
        target_number: str,
        *,
        transfer_message: str | None = None,
        whisper_url: str | None = None,
        action_url: str | None = None,
        amd_callback_url: str | None = None,
    ) -> str:
        """Build the transfer TwiML. See ADR-0009.

        - transfer_message → <Say> to the caller before the dial.
        - whisper_url / amd_callback_url → use a <Number> child so the operator
          leg gets a whisper (warm briefing) and/or answering-machine detection.
        - action_url → <Dial action> so a no-answer routes to the result handler.
        Plain cold transfer (no extras) stays a bare <Dial>target</Dial>.
        """
        response = ET.Element("Response")
        if transfer_message:
            say = ET.SubElement(response, "Say")
            say.text = transfer_message
        dial = ET.SubElement(response, "Dial")
        if action_url:
            dial.set("action", action_url)
            dial.set("method", "POST")
        if whisper_url or amd_callback_url:
            number = ET.SubElement(dial, "Number")
            number.text = target_number
            if whisper_url:
                number.set("url", whisper_url)
            if amd_callback_url:
                number.set("machineDetection", "Enable")
                number.set("amdStatusCallback", amd_callback_url)
                number.set("amdStatusCallbackMethod", "POST")
        else:
            dial.text = target_number
        return ET.tostring(response, encoding="unicode")


def _available_number(record: Any, number_type: str) -> dict[str, Any]:
    """An available Twilio number in the carrier-neutral shape a search
    returns. Twilio spells SMS and MMS upper-case in `capabilities`."""
    capabilities = {k.lower(): bool(v) for k, v in (record.capabilities or {}).items()}
    return {
        "e164_number": record.phone_number,
        "friendly_name": record.friendly_name,
        "country": record.iso_country,
        "region": record.region,
        "locality": record.locality,
        "postal_code": record.postal_code,
        "number_type": number_type,
        "capabilities": {
            "voice": capabilities.get("voice", False),
            "sms": capabilities.get("sms", False),
            "mms": capabilities.get("mms", False),
        },
    }


def _page_error(exc: TwilioException, uri: str) -> Exception:
    """A list request Twilio refused, as the TwilioRestException any other
    refused request is. The SDK's paging raises a bare TwilioException
    carrying the response instead, so a 404 or a 401 would read as neither."""
    response = exc.args[1] if len(exc.args) > 1 else None
    status = getattr(response, "status_code", None)
    if status is None:
        return exc
    try:
        body = json.loads(response.text)
    except (TypeError, ValueError):
        body = {}
    return TwilioRestException(
        status, uri, msg=body.get("message", ""), code=body.get("code")
    )
