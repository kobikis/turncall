"""An in-memory carrier: the REST side of telephony, recorded.

TurnCall talks to a carrier in two directions. Outbound it makes REST calls
(dial, hang up, transfer, DTMF, configure a number) through a
`TelephonyAdapter`; inbound the carrier posts webhooks and opens a media
stream. `FakeCarrier` is the first half — every REST action lands in
`actions` instead of on the network — plus the state the second half needs:
the numbers it owns and the URLs TurnCall configured on them. The inbound
edge is per carrier format (`twilio.py`).
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

from tests.harness.stubs import Setter
from turncall.adapters.telephony.base import TelephonyAdapter


@dataclass(frozen=True)
class CarrierAction:
    """One REST request TurnCall made. `account_sid` is the account it used."""

    kind: str  # dial | hang_up | transfer | send_dtmf | configure_number | configure_sms | ...
    account_sid: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeNumber:
    """A number on the fake carrier and the webhooks configured on it."""

    e164: str
    sid: str
    voice_url: str = ""
    status_callback_url: str = ""
    sms_url: str = ""


class FakeCarrier:
    """One fake Twilio-shaped carrier account (and any per-number accounts).

    `account_sid`/`auth_token` are the platform account the harness gives the
    app; webhooks are signed with that token.
    """

    def __init__(self) -> None:
        self.account_sid = "AC" + secrets.token_hex(16)
        self.auth_token = secrets.token_hex(16)
        self.actions: list[CarrierAction] = []
        self._numbers: dict[str, FakeNumber] = {}
        self._recorded = asyncio.Condition()

    # -- numbers ---------------------------------------------------------

    def add_number(self, e164: str) -> FakeNumber:
        """Own a number, as buying one would. Returns it with its PN SID."""
        number = FakeNumber(e164=e164, sid="PN" + secrets.token_hex(16))
        self._numbers[e164] = number
        return number

    def number(self, e164: str) -> FakeNumber:
        return self._numbers[e164]

    def _by_sid(self, sid: str) -> FakeNumber | None:
        return next((n for n in self._numbers.values() if n.sid == sid), None)

    # -- recording -------------------------------------------------------

    def actions_of(self, kind: str) -> list[CarrierAction]:
        return [a for a in self.actions if a.kind == kind]

    async def wait_for(self, kind: str, *, timeout: float = 5.0) -> CarrierAction:
        """The first `kind` action, waiting up to `timeout` for it to happen."""
        async with self._recorded:
            await asyncio.wait_for(
                self._recorded.wait_for(lambda: self.actions_of(kind)), timeout
            )
        return self.actions_of(kind)[0]

    async def record(self, kind: str, account_sid: str, **params: Any) -> None:
        self.actions.append(CarrierAction(kind, account_sid, params))
        async with self._recorded:
            self._recorded.notify_all()

    # -- installation ----------------------------------------------------

    def adapter(self, account_sid: str) -> FakeTelephonyAdapter:
        return FakeTelephonyAdapter(self, account_sid)

    def install(self, set_attr: Setter = setattr) -> None:
        """Route every adapter `services/twilio_accounts.py` hands out here.

        That module is the one place a Twilio adapter is constructed, for the
        platform account and per-number accounts alike, so this covers every
        REST call the app makes. Pass `monkeypatch.setattr` in tests.
        """
        from turncall.services.twilio_accounts import TwilioAccount

        carrier = self

        def adapter(account: TwilioAccount) -> FakeTelephonyAdapter:
            return carrier.adapter(account.account_sid)

        set_attr(TwilioAccount, "adapter", adapter)


class FakeTelephonyAdapter(TelephonyAdapter):
    """Records each REST action on its carrier and answers like Twilio would."""

    def __init__(self, carrier: FakeCarrier, account_sid: str) -> None:
        self._carrier = carrier
        self._account_sid = account_sid

    async def _record(self, kind: str, **params: Any) -> None:
        await self._carrier.record(kind, self._account_sid, **params)

    async def initiate_outbound_call(
        self,
        from_number: str,
        to_number: str,
        webhook_url: str,
        *,
        status_callback_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        call_sid = "CA" + secrets.token_hex(16)
        await self._record(
            "dial",
            call_sid=call_sid,
            from_number=from_number,
            to_number=to_number,
            webhook_url=webhook_url,
            status_callback_url=status_callback_url,
        )
        return call_sid

    async def end_call(self, provider_call_sid: str) -> None:
        await self._record("hang_up", call_sid=provider_call_sid)

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
        await self._record(
            "transfer",
            call_sid=provider_call_sid,
            target_number=target_number,
            transfer_message=transfer_message,
            whisper_url=whisper_url,
            action_url=action_url,
            amd_callback_url=amd_callback_url,
        )
        return provider_call_sid

    async def send_dtmf(self, provider_call_sid: str, digits: str) -> None:
        await self._record("send_dtmf", call_sid=provider_call_sid, digits=digits)

    async def get_recording_url(self, recording_sid: str) -> str:
        await self._record("get_recording_url", recording_sid=recording_sid)
        return f"https://fake-carrier.invalid/recordings/{recording_sid}.mp3"

    async def configure_number_webhook(
        self, number_sid: str, voice_url: str, status_callback_url: str
    ) -> None:
        await self._record(
            "configure_number",
            number_sid=number_sid,
            voice_url=voice_url,
            status_callback_url=status_callback_url,
        )
        number = self._carrier._by_sid(number_sid)
        if number is not None:
            number.voice_url, number.status_callback_url = (
                voice_url,
                status_callback_url,
            )

    async def fetch_number_info(self, number_sid: str) -> dict[str, Any]:
        number = self._carrier._by_sid(number_sid)
        return {
            "sid": number_sid,
            "phone_number": number.e164 if number else None,
            "voice_url": number.voice_url if number else None,
            "status_callback": number.status_callback_url if number else None,
        }

    async def find_number_sid(self, e164_number: str) -> str | None:
        number = self._carrier._numbers.get(e164_number)
        return number.sid if number else None

    async def send_sms(self, from_number: str, to_number: str, body: str) -> str:
        message_sid = "SM" + secrets.token_hex(16)
        await self._record(
            "send_sms",
            message_sid=message_sid,
            from_number=from_number,
            to_number=to_number,
            body=body,
        )
        return message_sid

    async def configure_sms_webhook(self, number_sid: str, sms_url: str) -> None:
        await self._record("configure_sms", number_sid=number_sid, sms_url=sms_url)
        number = self._carrier._by_sid(number_sid)
        if number is not None:
            number.sms_url = sms_url
