"""Telnyx as a carrier (#217, ADR-0022): its signature, its adapter, its
media stream and its credential. The end-to-end half is
`tests/integration/test_telnyx_inbound.py` and the carrier contract.
"""

import base64
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pipecat.serializers.telnyx import TelnyxFrameSerializer
from pydantic import ValidationError

from turncall.adapters.telephony.telnyx_adapter import (
    TelnyxAdapter,
    TelnyxApiError,
    TelnyxNumberError,
)
from turncall.api.errors import ApiError
from turncall.api.v1.schemas.credentials import parse_credential
from turncall.api.v1.schemas.phone_numbers import BindPhoneNumberRequest
from turncall.domain.carriers import CARRIERS
from turncall.domain.providers import CREDENTIAL_ACCOUNTS, account_for, accounts
from turncall.orchestrator import transport_factory as tf
from turncall.services import call_control
from turncall.services.telnyx_accounts import TelnyxAccount
from turncall.webhooks.telnyx_signature import verify_telnyx_signature

pytestmark = pytest.mark.unit


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, base64.b64encode(public).decode()


def _sign(private: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    return base64.b64encode(private.sign(timestamp.encode() + b"|" + body)).decode()


class TestTheSignature:
    """Ed25519 over `{timestamp}|{raw body}`, as Telnyx's SDKs verify it."""

    body = b'{"data": {"event_type": "call.initiated"}}'

    def test_a_webhook_signed_by_the_account_passes(self) -> None:
        private, public = _keypair()
        ts = str(int(time.time()))
        assert verify_telnyx_signature(
            public, _sign(private, ts, self.body), ts, self.body
        )

    def test_a_changed_body_fails(self) -> None:
        private, public = _keypair()
        ts = str(int(time.time()))
        signature = _sign(private, ts, self.body)
        assert not verify_telnyx_signature(public, signature, ts, self.body + b" ")

    def test_another_accounts_signature_fails(self) -> None:
        _, public = _keypair()
        other, _ = _keypair()
        ts = str(int(time.time()))
        assert not verify_telnyx_signature(
            public, _sign(other, ts, self.body), ts, self.body
        )

    def test_a_replay_outside_five_minutes_fails(self) -> None:
        """The signature is intact; only its age gives it away."""
        private, public = _keypair()
        ts = str(int(time.time()) - 301)
        assert not verify_telnyx_signature(
            public, _sign(private, ts, self.body), ts, self.body
        )

    def test_the_timestamp_is_part_of_what_was_signed(self) -> None:
        private, public = _keypair()
        ts = str(int(time.time()))
        signature = _sign(private, ts, self.body)
        assert not verify_telnyx_signature(
            public, signature, str(int(ts) - 1), self.body
        )

    @pytest.mark.parametrize(
        "public,signature,timestamp",
        [
            ("", "c2ln", "1"),  # no key: refused, never waved through
            ("not base64!", "c2ln", "1"),
            ("AAAA", "c2ln", "1"),  # not 32 bytes
            (None, "", "1"),  # unsigned
            (None, "c2ln", "yesterday"),
        ],
    )
    def test_anything_malformed_fails(self, public, signature, timestamp) -> None:
        public = _keypair()[1] if public is None else public
        assert not verify_telnyx_signature(public, signature, timestamp, self.body)


def _adapter(respond) -> tuple[TelnyxAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    return TelnyxAdapter("KEY123", transport=httpx.MockTransport(handler)), seen


class TestTheAdapter:
    async def test_it_answers_with_a_bidirectional_pcmu_stream(self) -> None:
        adapter, seen = _adapter(lambda _r: httpx.Response(200, json={"data": {}}))
        await adapter.answer_call(
            "v3:abc", stream_url="wss://x/ws/telnyx/media-stream/1"
        )
        request = seen[0]
        assert request.url == "https://api.telnyx.com/v2/calls/v3:abc/actions/answer"
        assert request.headers["authorization"] == "Bearer KEY123"
        assert json.loads(request.content) == {
            "stream_url": "wss://x/ws/telnyx/media-stream/1",
            "stream_track": "inbound_track",
            "stream_codec": "PCMU",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
        }

    async def test_it_hangs_up(self) -> None:
        adapter, seen = _adapter(lambda _r: httpx.Response(200, json={"data": {}}))
        await adapter.end_call("v3:abc")
        assert seen[0].url.path == "/v2/calls/v3:abc/actions/hangup"

    async def test_a_call_already_ended_counts_as_hung_up(self) -> None:
        """The caller hung up first — the usual case when a stream ends."""
        ended = {"errors": [{"code": "90018", "title": "Call has already ended"}]}
        adapter, _ = _adapter(lambda _r: httpx.Response(422, json=ended))
        await adapter.end_call("v3:abc")

    async def test_any_other_refusal_raises_with_its_status(self) -> None:
        refused = {"errors": [{"code": "10009", "detail": "Authentication failed"}]}
        adapter, _ = _adapter(lambda _r: httpx.Response(401, json=refused))
        with pytest.raises(TelnyxApiError) as exc:
            await adapter.end_call("v3:abc")
        assert exc.value.status == 401
        assert "Authentication failed" in str(exc.value)

    async def test_it_dials_on_the_caller_ids_application(self) -> None:
        """Telnyx's dial names a Call Control application: the caller-id
        number's own. Its filter matches partially, so the exact one is picked;
        the media goes straight to the stream once the callee answers."""
        numbers = {
            "data": [
                {"phone_number": "+155512345670", "connection_id": "other"},
                {"phone_number": "+15551234567", "connection_id": "app-1"},
            ]
        }

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=numbers)
            return httpx.Response(200, json={"data": {"call_control_id": "v3:new"}})

        adapter, seen = _adapter(respond)
        sid = await adapter.initiate_outbound_call(
            "+15551234567",
            "+15559876543",
            "https://tc.example/webhooks/telnyx/voice",
            stream_url="wss://tc.example/ws/telnyx/media-stream/1",
        )
        assert sid == "v3:new"
        lookup, dial = seen
        assert lookup.url.path == "/v2/phone_numbers"
        # Digits only: a `+` in Telnyx's filter matches nothing.
        assert lookup.url.params["filter[phone_number]"] == "15551234567"
        assert dial.url == "https://api.telnyx.com/v2/calls"
        assert json.loads(dial.content) == {
            "to": "+15559876543",
            "from": "+15551234567",
            "connection_id": "app-1",
            "webhook_url": "https://tc.example/webhooks/telnyx/voice",
            "stream_url": "wss://tc.example/ws/telnyx/media-stream/1",
            "stream_track": "inbound_track",
            "stream_codec": "PCMU",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
        }

    @pytest.mark.parametrize(
        "numbers,reason",
        [
            ([], "not on this Telnyx account"),
            (
                [{"phone_number": "+15551234567", "connection_id": None}],
                "no Call Control application",
            ),
        ],
    )
    async def test_a_number_it_cannot_dial_from_is_named(self, numbers, reason) -> None:
        adapter, seen = _adapter(lambda _r: httpx.Response(200, json={"data": numbers}))
        with pytest.raises(TelnyxNumberError, match=reason):
            await adapter.initiate_outbound_call("+15551234567", "+15559876543", "u")
        assert [r.method for r in seen] == ["GET"]  # nothing dialled

    async def test_a_given_application_is_dialled_on_without_a_lookup(self) -> None:
        adapter, seen = _adapter(
            lambda _r: httpx.Response(200, json={"data": {"call_control_id": "v3:x"}})
        )
        await adapter.initiate_outbound_call(
            "+15551234567", "+15559876543", "u", connection_id="app-9"
        )
        assert [r.method for r in seen] == ["POST"]
        assert json.loads(seen[0].content)["connection_id"] == "app-9"

    async def test_what_later_tickets_bring_raises_rather_than_pretends(self) -> None:
        adapter, seen = _adapter(lambda _r: httpx.Response(200))
        with pytest.raises(NotImplementedError, match="#220"):
            await adapter.transfer_call("v3:abc", "+15550000000")
        assert seen == []


class TestTheMediaStream:
    def test_telnyx_gets_pipecats_serializer_without_its_own_hangup(self) -> None:
        """Hanging up is a Telnyx REST command, and those are the adapter's."""
        transport = tf.create_carrier_transport(
            "telnyx", MagicMock(), tf.CarrierStream("S1", "v3:abc", "PCMA")
        )
        serializer = transport._params.serializer
        assert isinstance(serializer, TelnyxFrameSerializer)
        assert serializer._stream_id == "S1"
        assert serializer._params.auto_hang_up is False
        assert serializer._params.outbound_encoding == "PCMA"  # what Telnyx sends
        assert serializer._params.inbound_encoding == "PCMU"  # what we send
        assert transport._params.audio_in_sample_rate == tf.TELNYX_SAMPLE_RATE

    @pytest.mark.parametrize("carrier", CARRIERS)
    def test_every_carrier_carries_the_calls_ambience(self, carrier) -> None:
        from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

        mixer = SoundfileMixer(sound_files={}, default_sound="ambience")
        transport = tf.create_carrier_transport(
            carrier, MagicMock(), tf.CarrierStream("S1"), mixer=mixer
        )
        assert transport._params.audio_out_mixer is mixer


class TestTheCredential:
    def test_telnyx_is_an_account_and_twilio_is_not(self) -> None:
        """Twilio's account rides on the number (ADR-0022), not the project."""
        assert accounts()["telnyx"] == {"carrier": ["telnyx"]}
        assert account_for("twilio") is None
        assert "twilio" not in CREDENTIAL_ACCOUNTS

    def test_it_takes_the_key_and_the_webhook_public_key(self) -> None:
        public = _keypair()[1]
        doc = parse_credential("telnyx", {"api_key": " KEY ", "public_key": public})
        assert doc == {"api_key": "KEY", "public_key": public}

    @pytest.mark.parametrize(
        "body",
        [
            {"api_key": "KEY"},  # its webhooks could never be verified
            {"api_key": "KEY", "public_key": "AAAA"},
            {"api_key": "KEY", "public_key": "not base64!"},
        ],
    )
    def test_without_a_usable_public_key_it_is_refused(self, body) -> None:
        with pytest.raises(ApiError) as exc:
            parse_credential("telnyx", body)
        assert exc.value.status_code == 422


class TestCallControlActsOnTheCallsCarrier:
    async def test_a_telnyx_call_is_hung_up_through_telnyx(self) -> None:
        call = SimpleNamespace(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            provider="telnyx",
            provider_call_sid="v3:abc",
            status="in_progress",
            started_at=None,
        )
        adapter = SimpleNamespace(end_call=AsyncMock())
        account = MagicMock(spec=TelnyxAccount, api_key="KEY")
        account.adapter.return_value = adapter
        twilio = AsyncMock()
        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch("turncall.storage.repositories.call_repo.update_call_status"),
            patch.object(call_control, "_record_control_end", AsyncMock()),
            patch(
                "turncall.services.telnyx_accounts.account_for_project",
                AsyncMock(return_value=account),
            ),
            patch("turncall.services.twilio_accounts.account_for_call", twilio),
            patch(
                "turncall.services.call_analysis_trigger.config_for_call",
                AsyncMock(return_value=None),
            ),
        ):
            result = await call_control.end_call(AsyncMock(), call.id)
        assert result.success, result.message
        adapter.end_call.assert_awaited_once_with("v3:abc")
        twilio.assert_not_awaited()


BIND = {
    "carrier": "telnyx",
    "external_number_sid": "1293384261075731499",
    "e164_number": "+15551234567",
    "routing_target_type": "agent",
    "routing_target_id": str(uuid.uuid4()),
}


class TestTwilioOnlyNumberFeatures:
    def test_a_telnyx_number_binds(self) -> None:
        assert BindPhoneNumberRequest(**BIND).carrier == "telnyx"

    @pytest.mark.parametrize(
        "extra",
        [
            {"sms_enabled": True},
            {"twilio_account_sid": "AC" + "0" * 32, "twilio_auth_token": "t"},
        ],
        ids=["sms", "own-twilio-account"],
    )
    def test_are_refused_on_a_telnyx_number(self, extra) -> None:
        """Stored, they would never be used — and read as working."""
        with pytest.raises(ValidationError, match="telnyx"):
            BindPhoneNumberRequest(**BIND, **extra)
