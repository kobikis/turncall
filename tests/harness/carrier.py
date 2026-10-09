"""An in-memory carrier: the REST side of telephony, recorded.

TurnCall talks to a carrier in two directions. Outbound it makes REST calls
(dial, hang up, transfer, DTMF, configure a number) through a
`TelephonyAdapter`; inbound the carrier posts webhooks and opens a media
stream. `FakeCarrier` is the first half — every REST action lands in
`actions` instead of on the network — plus the state the second half needs:
the numbers it owns and the URLs TurnCall configured on them. The inbound
edge is per carrier format (`twilio.py`, `telnyx.py`).

One fake stands in for every carrier: a Telnyx command lands in the same
`actions` list as a Twilio one, under the same kind (`hang_up` is a hang-up on
either), so a contract test reads one list whatever carrier it ran on.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

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
    # Telnyx: the Call Control application the number is on — set by hand in
    # the console, as an operator does before #219 configures it on bind.
    connection_id: str = "harness-connection"


class FakeCarrier:
    """The platform's carrier accounts (and any per-number Twilio accounts).

    `account_sid`/`auth_token` are the Twilio account the harness gives the
    app; Twilio webhooks are signed with that token. `telnyx_api_key` and
    `telnyx_public_key` are the Telnyx account's; Telnyx webhooks are signed
    with `telnyx_signing_key`, the private half, as Telnyx signs them.
    """

    def __init__(self) -> None:
        self.account_sid = "AC" + secrets.token_hex(16)
        self.auth_token = secrets.token_hex(16)
        self.telnyx_api_key = "KEY" + secrets.token_hex(16)
        self.telnyx_signing_key = Ed25519PrivateKey.generate()
        self.telnyx_public_key = base64.b64encode(
            self.telnyx_signing_key.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            )
        ).decode()
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
        """Route every adapter the app constructs here.

        `services/twilio_accounts.py` and `services/telnyx_accounts.py` are the
        one place each carrier's adapter is built, for every account, so this
        covers every REST call the app makes. Pass `monkeypatch.setattr` in
        tests.
        """
        from turncall.services.telnyx_accounts import TelnyxAccount
        from turncall.services.twilio_accounts import TwilioAccount

        carrier = self

        def adapter(account: TwilioAccount) -> FakeTelephonyAdapter:
            return carrier.adapter(account.account_sid)

        def telnyx_adapter(account: TelnyxAccount) -> FakeTelnyxAdapter:
            return FakeTelnyxAdapter(carrier, account.api_key)

        set_attr(TwilioAccount, "adapter", adapter)
        set_attr(TelnyxAccount, "adapter", telnyx_adapter)


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


class FakeTelnyxAdapter(FakeTelephonyAdapter):
    """Telnyx's commands, recorded. `account_sid` on its actions is the API key
    the app used. `answer` and `reject` are Telnyx's own: Call Control answers
    or refuses a call with a REST command where Twilio reads TwiML."""

    async def answer_call(self, call_control_id: str, *, stream_url: str) -> None:
        await self._record("answer", call_sid=call_control_id, stream_url=stream_url)

    async def reject_call(
        self, call_control_id: str, *, cause: str = "USER_BUSY"
    ) -> None:
        await self._record("reject", call_sid=call_control_id, cause=cause)

    async def connection_for(self, e164: str) -> str:
        """The Call Control application the number is on; Telnyx refuses a
        dial on none."""
        from turncall.adapters.telephony.telnyx_adapter import TelnyxNumberError

        number = self._carrier._numbers.get(e164)
        if number is None or not number.connection_id:
            raise TelnyxNumberError(f"{e164} is on no Call Control application")
        return number.connection_id

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
        """A Telnyx dial, on `connection_id` or the caller id's application."""
        call_control_id = "v3:" + secrets.token_urlsafe(24)
        await self._record(
            "dial",
            call_sid=call_control_id,
            from_number=from_number,
            to_number=to_number,
            webhook_url=webhook_url,
            status_callback_url=status_callback_url,
            stream_url=stream_url,
            connection_id=connection_id or await self.connection_for(from_number),
        )
        return call_control_id
