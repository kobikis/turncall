"""Twilio telephony adapter implementation."""

import xml.etree.ElementTree as ET
from typing import Any
from uuid import UUID

from loguru import logger
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from turncall.adapters.telephony.base import TelephonyAdapter


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
        """Validate a Twilio webhook signature."""
        return self._validator.validate(url, params, signature)

    async def get_recording_url(self, recording_sid: str) -> str:
        """Get the URL for a call recording."""
        recording = self._client.recordings(recording_sid).fetch()
        return f"https://api.twilio.com{recording.uri.replace('.json', '.mp3')}"

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

    def generate_media_stream_twiml(
        self,
        websocket_url: str,
        call_id: UUID,
    ) -> str:
        """Generate TwiML to start a bidirectional media stream."""
        response = ET.Element("Response")
        connect = ET.SubElement(response, "Connect")
        stream = ET.SubElement(connect, "Stream", url=websocket_url)
        ET.SubElement(stream, "Parameter", name="call_id", value=str(call_id))
        return ET.tostring(response, encoding="unicode")

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
