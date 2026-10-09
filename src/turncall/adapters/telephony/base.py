"""Abstract telephony adapter interface."""

from abc import ABC, abstractmethod
from typing import Any


class TelephonyAdapter(ABC):
    """A carrier's REST actions — the ones every carrier has (ADR-0022).

    Only what TurnCall asks of a carrier: dial, hang up, transfer, DTMF,
    configure a number. How a carrier signs its webhooks and answers a call
    (TwiML, a Call Control command, an ARI channel) belongs to its inbound
    edge under `webhooks/`, never here.
    """

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
        whisper_url: warm transfer — the briefing the operator hears before
            bridging (each carrier delivers it its own way).
        action_url: dial-result callback (graceful fallback on no-answer).
        amd_callback_url: answering-machine-detection notify callback.
        """

    @abstractmethod
    async def send_dtmf(self, provider_call_sid: str, digits: str) -> None:
        """Send DTMF tones on an active call."""

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
