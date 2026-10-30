"""Asterisk ARI as a carrier (#209): the parts the contract test cannot see.

The end-to-end half — an inbound call through a fake ARI server, hang-up,
cold transfer, DTMF — is `tests/integration/test_carrier_contract.py`. Here:
the chan_websocket wire format, what a transfer refuses rather than does
differently, how an Asterisk number and credential are validated, and the ARI
edge's bookkeeping when a caller leaves early or dials a number it cannot take.
"""

import asyncio
import audioop
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pydantic import ValidationError

from turncall.adapters.telephony.asterisk_adapter import AriAccount, AsteriskAdapter
from turncall.adapters.telephony.base import UnsupportedCarrierAction
from turncall.api.errors import ApiError
from turncall.api.v1.schemas.credentials import parse_credential
from turncall.api.v1.schemas.phone_numbers import BindPhoneNumberRequest
from turncall.orchestrator import transport_factory as tf
from turncall.orchestrator.asterisk_serializer import AsteriskFrameSerializer
from turncall.services.inbound_call import InboundRefusal
from turncall.services.media_token import media_token
from turncall.webhooks import asterisk_edge
from turncall.webhooks.asterisk_media_stream import (
    media_channel_id,
    media_channel_parts,
)

pytestmark = pytest.mark.unit


class TestTheMediaWireFormat:
    async def test_caller_audio_is_raw_ulaw_at_8k(self) -> None:
        pcm = b"\x10\x00" * 160
        frame = await AsteriskFrameSerializer().deserialize(audioop.lin2ulaw(pcm, 2))
        assert isinstance(frame, InputAudioRawFrame)
        assert frame.sample_rate == 8000
        assert len(frame.audio) == len(pcm)

    async def test_agent_audio_goes_out_in_whole_20ms_frames(self) -> None:
        """chan_websocket re-frames what it is sent; a partial frame risks
        being dropped, so the tail is padded with ulaw silence."""
        out = await AsteriskFrameSerializer().serialize(
            OutputAudioRawFrame(
                audio=b"\x00\x01" * 250, sample_rate=8000, num_channels=1
            )
        )
        assert isinstance(out, bytes)
        assert len(out) == 320
        assert out[250:] == b"\xff" * 70

    async def test_a_barge_in_flushes_what_asterisk_has_queued(self) -> None:
        assert await AsteriskFrameSerializer().serialize(InterruptionFrame()) == (
            "FLUSH_MEDIA"
        )

    async def test_a_keypress_is_dtmf_input(self) -> None:
        frame = await AsteriskFrameSerializer().deserialize(
            json.dumps({"event": "DTMF_END", "digit": "5"})
        )
        assert isinstance(frame, InputDTMFFrame)
        assert frame.button.value == "5"

    @pytest.mark.parametrize(
        "text", ['{"event": "MEDIA_XOFF"}', "MEDIA_XON", '{"event": "DTMF_END"}']
    )
    async def test_other_control_messages_are_not_frames(self, text: str) -> None:
        assert await AsteriskFrameSerializer().deserialize(text) is None

    def test_the_media_entry_point_builds_it_for_an_asterisk_call(self) -> None:
        transport = tf.create_carrier_transport(
            "asterisk", AsyncMock(), tf.CarrierStream("conn-1")
        )
        assert isinstance(transport._params.serializer, AsteriskFrameSerializer)
        assert transport._params.audio_out_sample_rate == 8000

    def test_the_media_channel_id_carries_the_call_id_and_its_token(self) -> None:
        call_id = uuid.uuid4()
        channel = media_channel_id(call_id)
        assert media_channel_parts(channel) == (call_id, media_token(call_id))
        # Asterisk's channel ids are at most 150 characters (AST_MAX_UNIQUEID).
        assert len(channel) <= 150
        assert media_channel_parts(f"turncall-media-{call_id}") == (call_id, "")
        assert media_channel_parts("PJSIP/trunk-0001") is None
        assert media_channel_parts("turncall-media-not-a-uuid") is None


ACCOUNT = AriAccount(url="http://pbx:8088", username="u", password="p")


class TestATransferIsColdOnly:
    """One behaviour per transfer config (ADR-0022): what ARI cannot do the
    way Twilio does is refused, never done differently."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"whisper_url": "https://x/whisper"},
            {"transfer_message": "Putting you through"},
            {"action_url": "https://x/result"},
        ],
    )
    async def test_what_it_cannot_do_is_refused(self, kwargs) -> None:
        adapter = AsteriskAdapter(ACCOUNT)
        with (
            patch.object(adapter, "request", AsyncMock()) as request,
            pytest.raises(UnsupportedCarrierAction),
        ):
            await adapter.transfer_call("chan-1", "+15559990000", **kwargs)
        request.assert_not_awaited()

    async def test_a_cold_transfer_continues_in_the_transfer_context(self) -> None:
        adapter = AsteriskAdapter(ACCOUNT)
        with patch.object(adapter, "request", AsyncMock()) as request:
            await adapter.transfer_call(
                "chan-1", "+15559990000", amd_callback_url="https://x/amd"
            )
        request.assert_awaited_once_with(
            "POST",
            "channels/chan-1/continue",
            params={
                "context": "turncall-transfer",
                "extension": "+15559990000",
                "priority": 1,
            },
        )

    async def test_the_model_is_told_why(self) -> None:
        from turncall.services import call_control

        call = SimpleNamespace(
            status="in_progress",
            provider="asterisk",
            provider_call_sid="chan-1",
            project_id=uuid.uuid4(),
        )
        adapter = AsteriskAdapter(ACCOUNT)
        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch("turncall.storage.repositories.call_repo.update_call_status"),
            patch("turncall.events.emit.emit_call_event", AsyncMock()),
            patch.object(
                call_control, "_carrier_adapter", AsyncMock(return_value=adapter)
            ),
            # Parked only with a public URL, which an earlier test's settings
            # may still carry: not what this test is about, and needs Redis.
            patch("turncall.services.transfer.store_transfer_intent", AsyncMock()),
        ):
            result = await call_control.transfer_call(
                AsyncMock(),
                uuid.uuid4(),
                target_number="+15559990000",
                transfer_message="One moment",
            )
        assert result.success is False
        assert "transfer_message" in result.message


class TestAsteriskNumbersAndCredentials:
    def test_a_number_needs_no_sid(self) -> None:
        body = BindPhoneNumberRequest(
            carrier="asterisk",
            e164_number="+15551230000",
            routing_target_type="agent",
            routing_target_id=uuid.uuid4(),
        )
        assert body.external_number_sid is None

    def test_a_number_takes_no_sms(self) -> None:
        with pytest.raises(ValidationError, match="sms_enabled is not supported"):
            BindPhoneNumberRequest(
                carrier="asterisk",
                e164_number="+15551230000",
                routing_target_type="agent",
                routing_target_id=uuid.uuid4(),
                sms_enabled=True,
            )

    def test_a_credential_is_a_url_and_a_login(self) -> None:
        stored = parse_credential(
            "asterisk",
            {
                "ari_url": "https://pbx.example.com:8089/",
                "username": "u",
                "password": "p",
            },
        )
        assert stored == {
            "ari_url": "https://pbx.example.com:8089",
            "username": "u",
            "password": "p",
            "app": "turncall",
            "media_connection": "turncall",
            "transfer_context": "turncall-transfer",
        }

    @pytest.mark.parametrize(
        "body",
        [
            {"ari_url": "pbx:8088", "username": "u", "password": "p"},
            {"ari_url": "http://pbx:8088", "username": "u"},
            {"api_key": "k"},
        ],
    )
    def test_a_malformed_credential_is_refused(self, body) -> None:
        with pytest.raises(ApiError) as caught:
            parse_credential("asterisk", body)
        assert caught.value.status_code == 422

    def test_its_url_passes_the_ssrf_gate(self, monkeypatch) -> None:
        """TurnCall connects out to it, like an MCP server or a BYOM endpoint."""
        from turncall.config.settings import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("BYOM_ALLOWED_URL_PATTERNS", '["https://*.trusted.com/*"]')
        try:
            with pytest.raises(ApiError):
                parse_credential(
                    "asterisk",
                    {"ari_url": "https://evil.com", "username": "u", "password": "p"},
                )
        finally:
            get_settings.cache_clear()


class _Adapter:
    """Records the edge's ARI requests."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        async def record(*args, **kwargs):
            self.calls.append((name, *args))

        return record


def _edge(admitted) -> tuple[asterisk_edge.AriEdge, _Adapter]:
    edge = asterisk_edge.AriEdge(ACCOUNT, frozenset({None}))
    adapter = _Adapter()
    edge._adapter = adapter  # type: ignore[assignment]
    edge._admit_call = AsyncMock(return_value=admitted)  # type: ignore[method-assign]
    return edge, adapter


def _stasis_start(channel_id: str = "chan-1") -> dict:
    return {
        "type": "StasisStart",
        "channel": {
            "id": channel_id,
            "caller": {"number": "15550001234"},
            "dialplan": {"exten": "15551230000"},
        },
        "args": [],
    }


async def _settle(edge: asterisk_edge.AriEdge) -> None:
    while edge._tasks:
        await asyncio.gather(*list(edge._tasks))


class TestTheAriEdge:
    async def test_a_call_it_cannot_take_goes_back_to_the_dialplan(self) -> None:
        edge, adapter = _edge(InboundRefusal("This number is not configured."))
        edge._dispatch(_stasis_start())
        await _settle(edge)
        assert adapter.calls == [("continue_in_dialplan", "chan-1")]
        assert edge._calls == {}

    async def test_the_dialled_number_and_caller_are_read_as_e164(self) -> None:
        edge, _ = _edge(InboundRefusal("no"))
        edge._dispatch(_stasis_start())
        await _settle(edge)
        edge._admit_call.assert_awaited_once_with(  # type: ignore[attr-defined]
            "chan-1", "+15551230000", "+15550001234"
        )

    async def test_a_caller_who_leaves_before_the_media_is_torn_down(self) -> None:
        """Queued behind admission by the call's lock, not lost; the call row,
        which no pipeline will end, is ended here."""
        call_id, project_id = uuid.uuid4(), uuid.uuid4()
        edge, adapter = _edge((call_id, project_id))
        abandoned = AsyncMock()
        with patch.object(edge, "_abandoned", abandoned):
            edge._dispatch(_stasis_start())
            edge._dispatch({"type": "StasisEnd", "channel": {"id": "chan-1"}})
            await _settle(edge)
        media = media_channel_id(call_id)
        assert [c[0] for c in adapter.calls] == [
            "set_variable",
            "answer",
            "external_media",
            "end_call",
        ]
        assert adapter.calls[-1] == ("end_call", media)
        abandoned.assert_awaited_once()
        assert edge._calls == {}

    async def test_the_media_ending_hangs_up_the_caller(self) -> None:
        call_id = uuid.uuid4()
        edge, adapter = _edge((call_id, uuid.uuid4()))
        media = media_channel_id(call_id)
        edge._dispatch(_stasis_start())
        await _settle(edge)  # Asterisk dials the media only once asked to
        edge._dispatch({"type": "Dial", "peer": {"id": media}, "dialstatus": "ANSWER"})
        edge._dispatch({"type": "StasisEnd", "channel": {"id": media}})
        edge._dispatch({"type": "StasisEnd", "channel": {"id": "chan-1"}})
        await _settle(edge)
        names = [c[0] for c in adapter.calls]
        assert names[3:] == ["bridge", "end_call", "destroy_bridge"]
        assert adapter.calls[4] == ("end_call", "chan-1")
        assert edge._calls == {}

    async def test_a_transferred_caller_is_never_hung_up(self) -> None:
        """A cold transfer continues the caller in the dialplan: they leave
        Stasis first, and only the media channel is hung up."""
        call_id = uuid.uuid4()
        edge, adapter = _edge((call_id, uuid.uuid4()))
        media = media_channel_id(call_id)
        edge._dispatch(_stasis_start())
        await _settle(edge)  # Asterisk dials the media only once asked to
        edge._dispatch({"type": "Dial", "peer": {"id": media}, "dialstatus": "ANSWER"})
        edge._dispatch({"type": "StasisEnd", "channel": {"id": "chan-1"}})
        edge._dispatch({"type": "StasisEnd", "channel": {"id": media}})
        await _settle(edge)
        hangups = [c[1] for c in adapter.calls if c[0] == "end_call"]
        assert hangups == [media]
