"""Abstract telephony adapter interface."""

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID


class TelephonyAdapter(ABC):
    """Interface for telephony providers (Twilio, etc.)."""

    @abstractmethod
    async def initiate_outbound_call(
        self,
        from_number: str,
        to_number: str,
        webhook_url: str,
        *,
        status_callback_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Initiate an outbound call. Returns provider call SID."""

    @abstractmethod
    async def end_call(self, provider_call_sid: str) -> None:
        """Hang up an active call."""

    @abstractmethod
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
        """Transfer a call to another number. Returns the call SID.

        transfer_message: spoken to the caller before the dial.
        whisper_url: warm transfer — TwiML played to the operator before bridging.
        action_url: dial-result callback (graceful fallback on no-answer).
        amd_callback_url: answering-machine-detection notify callback.
        """

    @abstractmethod
    async def send_dtmf(self, provider_call_sid: str, digits: str) -> None:
        """Send DTMF tones on an active call."""

    @abstractmethod
    def validate_webhook_signature(
        self,
        signature: str,
        url: str,
        params: dict[str, str],
    ) -> bool:
        """Validate an incoming webhook signature."""

    @abstractmethod
    async def get_recording_url(self, recording_sid: str) -> str:
        """Get the URL for a call recording."""

    @abstractmethod
    async def configure_number_webhook(
        self,
        number_sid: str,
        voice_url: str,
        status_callback_url: str,
    ) -> None:
        """Configure webhook URLs on a phone number."""

    @abstractmethod
    async def fetch_number_info(self, number_sid: str) -> dict[str, Any]:
        """Fetch phone number details from the provider."""

    @abstractmethod
    def generate_media_stream_twiml(
        self,
        websocket_url: str,
        call_id: UUID,
    ) -> str:
        """Generate TwiML to start a media stream for a call."""

    @abstractmethod
    async def send_sms(
        self,
        from_number: str,
        to_number: str,
        body: str,
    ) -> str:
        """Send an SMS message. Returns provider message SID."""

    @abstractmethod
    async def configure_sms_webhook(
        self,
        number_sid: str,
        sms_url: str,
    ) -> None:
        """Configure the SMS webhook URL on a phone number."""
