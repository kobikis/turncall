"""An in-memory carrier: the REST side of telephony, recorded.

TurnCall talks to a carrier in two directions. Outbound it makes REST calls
(dial, hang up, transfer, DTMF, configure a number) through a
`TelephonyAdapter`; inbound the carrier posts webhooks and opens a media
stream. `FakeCarrier` is the first half — every REST action lands in
`actions` instead of on the network — plus the state the second half needs:
the numbers it owns and the URLs TurnCall configured on them, and the numbers
it has for sale (`offer`: a search lists them, `buy_number` moves one onto the
account). The inbound edge is per carrier format (`twilio.py`, `telnyx.py`).

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

    # dial | hang_up | transfer | send_dtmf | configure_number | configure_sms |
    # buy_number | ...
    kind: str
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
    # Telnyx: the Call Control application the number is on, which binding
    # it sets (#219); `voice_url` is then that application's webhook URL.
    connection_id: str = ""


@dataclass(frozen=True)
class OfferedNumber:
    """A number the fake carrier has for sale, as a search finds it."""

    e164: str
    country: str = "US"
    number_type: str = "local"
    area_code: str = ""
    locality: str = "Springfield"
    region: str = "IL"

    def matches(
        self,
        country: str,
        number_type: str,
        area_code: str | None,
        contains: str | None,
    ) -> bool:
        return (
            self.country == country
            and self.number_type == number_type
            and (not area_code or self.area_code == area_code)
            and (not contains or contains in self.e164)
        )


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
        self._for_sale: dict[str, OfferedNumber] = {}
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

    def offer(self, e164: str, **fields: Any) -> OfferedNumber:
        """Put a number up for sale: a search can list it and a buy takes it.
        A +1 number's area code is its next three digits unless given."""
        if e164.startswith("+1"):
            fields.setdefault("area_code", e164[2:5])
        offered = OfferedNumber(e164=e164, **fields)
        self._for_sale[e164] = offered
        return offered

    def for_sale(self) -> list[OfferedNumber]:
        return list(self._for_sale.values())

    def _sell(self, e164: str) -> FakeNumber | None:
        """Move a number from the shelf onto the account; None if it is not
        for sale (already sold, or never offered)."""
        if self._for_sale.pop(e164, None) is None:
            return None
        return self.add_number(e164)

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

    async def action_at(self, index: int, *, timeout: float = 5.0) -> CarrierAction:
        """The `index`-th action, waiting up to `timeout` for it to happen. For
        a carrier that answers each command before the app issues the next."""
        async with self._recorded:
            await asyncio.wait_for(
                self._recorded.wait_for(lambda: len(self.actions) > index), timeout
            )
        return self.actions[index]

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

    async def search_numbers(
        self,
        country: str,
        *,
        number_type: str = "local",
        area_code: str | None = None,
        contains: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """The numbers on sale matching the filters, as `TwilioAdapter`
        returns them. A read: no action is recorded."""
        found = [
            n
            for n in self._carrier.for_sale()
            if n.matches(country, number_type, area_code, contains)
        ]
        return [
            {
                "e164_number": n.e164,
                "friendly_name": n.e164,
                "country": n.country,
                "region": n.region,
                "locality": n.locality,
                "postal_code": None,
                "number_type": n.number_type,
                "capabilities": {"voice": True, "sms": True, "mms": False},
            }
            for n in found[:limit]
        ]

    async def buy_number(self, e164_number: str) -> dict[str, Any]:
        """Buy a number on sale onto the account, recorded as `buy_number`.
        One not on sale is refused as Twilio refuses it: a 400, code 21422."""
        from twilio.base.exceptions import TwilioRestException

        number = self._carrier._sell(e164_number)
        if number is None:
            raise TwilioRestException(
                400,
                "/IncomingPhoneNumbers.json",
                msg=f"Phone number {e164_number} is not available",
                code=21422,
                method="POST",
            )
        await self._record("buy_number", e164=number.e164, number_sid=number.sid)
        return {
            "number_sid": number.sid,
            "e164_number": number.e164,
            "friendly_name": number.e164,
            "status": "active",
        }

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


# Every field a Telnyx dial's action carries: an outbound call's webhook and
# stream, a transfer leg's link, state and machine detection.
_DIAL_FIELDS = (
    "connection_id",
    "from_number",
    "to_number",
    "webhook_url",
    "status_callback_url",
    "stream_url",
    "link_to",
    "client_state",
    "detect_machine",
)


class FakeTelnyxAdapter(FakeTelephonyAdapter):
    """Telnyx's commands, recorded. `account_sid` on its actions is the API key
    the app used. `answer` and `reject` are Telnyx's own: Call Control answers
    or refuses a call with a REST command where Twilio reads TwiML. So is a
    transfer's sequence (`stop_streaming`, `speak`, `start_playback`,
    `stop_playback`, `dial`, `bridge`); `telnyx.TelnyxCall.complete_transfer`
    plays Telnyx's side of it."""

    async def answer_call(self, call_control_id: str, *, stream_url: str) -> None:
        await self._record("answer", call_sid=call_control_id, stream_url=stream_url)

    async def reject_call(
        self, call_control_id: str, *, cause: str = "USER_BUSY"
    ) -> None:
        await self._record("reject", call_sid=call_control_id, cause=cause)

    async def transfer_call(self, provider_call_sid: str, *args: Any, **kw: Any) -> str:
        raise NotImplementedError("a Telnyx transfer is a sequence of commands")

    async def stop_streaming(self, call_control_id: str, *, client_state: str) -> None:
        await self._record(
            "stop_streaming", call_sid=call_control_id, client_state=client_state
        )

    async def speak(
        self, call_control_id: str, text: str, *, client_state: str
    ) -> None:
        await self._record(
            "speak", call_sid=call_control_id, text=text, client_state=client_state
        )

    async def start_playback(self, call_control_id: str, wav: bytes) -> None:
        await self._record("start_playback", call_sid=call_control_id, wav=wav)

    async def stop_playback(self, call_control_id: str) -> None:
        await self._record("stop_playback", call_sid=call_control_id)

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
        return await self._dial(
            connection_id=connection_id,
            to_number=to,
            from_number=from_number,
            link_to=link_to,
            client_state=client_state,
            detect_machine=detect_machine,
        )

    async def bridge(
        self, call_control_id: str, other: str, *, client_state: str
    ) -> None:
        await self._record(
            "bridge", call_sid=call_control_id, other=other, client_state=client_state
        )

    async def find_number(self, e164: str) -> dict[str, Any] | None:
        number = self._carrier._numbers.get(e164)
        if number is None:
            return None
        return {
            "id": number.sid,
            "phone_number": number.e164,
            "connection_id": number.connection_id or None,
        }

    async def configure_number_webhook(
        self, number_sid: str, voice_url: str, status_callback_url: str
    ) -> None:
        """Telnyx routes a number by its Call Control application: put it on
        the one named for `voice_url`'s host, or take it off ("")."""
        from turncall.adapters.telephony.telnyx_adapter import application_name

        await self._record(
            "configure_number",
            number_sid=number_sid,
            voice_url=voice_url,
            status_callback_url=status_callback_url,
        )
        number = self._carrier._by_sid(number_sid)
        if number is not None:
            number.voice_url = voice_url
            number.connection_id = application_name(voice_url) if voice_url else ""

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
        return await self._dial(
            connection_id=connection_id or await self.connection_for(from_number),
            to_number=to_number,
            from_number=from_number,
            webhook_url=webhook_url,
            status_callback_url=status_callback_url,
            stream_url=stream_url,
        )

    async def _dial(self, **params: Any) -> str:
        """`POST /calls`, whoever asks: an outbound call (#218) or a transfer's
        operator leg (#220). One `dial` action with every field either sends,
        absent ones None, so a test reads either the same way."""
        call_control_id = "v3:" + secrets.token_urlsafe(24)
        fields = dict.fromkeys(_DIAL_FIELDS) | params
        await self._record("dial", call_sid=call_control_id, **fields)
        return call_control_id
