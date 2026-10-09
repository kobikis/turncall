"""A Telnyx transfer's commands and its start (#220, ADR-0009 on Call Control).

The whole conversation with Telnyx's webhooks runs end to end in the carrier
contract (`tests/integration/test_carrier_contract.py`); this pins the wire
format of each command against Telnyx's API reference, and what a transfer
that cannot start leaves behind.
"""

import io
import json
import uuid
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from turncall.adapters.telephony.telnyx_adapter import TelnyxAdapter
from turncall.services import call_control, telnyx_transfer
from turncall.services.telnyx_accounts import TelnyxAccount
from turncall.services.transfer import TransferIntent

pytestmark = pytest.mark.unit


def _adapter(data: dict | None = None) -> tuple[TelnyxAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": data or {"result": "ok"}})

    return TelnyxAdapter("KEY", transport=httpx.MockTransport(handler)), seen


def _sent(request: httpx.Request) -> tuple[str, dict]:
    return request.url.path, json.loads(request.content)


class TestTheCommands:
    async def test_dtmf(self) -> None:
        adapter, seen = _adapter()
        await adapter.send_dtmf("v3:a", "1w#")
        assert _sent(seen[0]) == ("/v2/calls/v3:a/actions/send_dtmf", {"digits": "1w#"})

    async def test_stopping_the_agents_stream(self) -> None:
        adapter, seen = _adapter()
        await adapter.stop_streaming("v3:a", client_state="cw==")
        assert _sent(seen[0]) == (
            "/v2/calls/v3:a/actions/streaming_stop",
            {"client_state": "cw=="},
        )

    async def test_speaking_in_telnyxs_voice(self) -> None:
        adapter, seen = _adapter()
        await adapter.speak("v3:a", "x" * 3500, client_state="cw==")
        path, body = _sent(seen[0])
        assert path == "/v2/calls/v3:a/actions/speak"
        assert body == {
            "payload": "x" * 3000,  # Telnyx's limit
            "voice": "AWS.Polly.Joanna",
            "client_state": "cw==",
        }

    async def test_dialling_the_operator_on_a_linked_leg(self) -> None:
        adapter, seen = _adapter({"call_control_id": "v3:b", "is_alive": True})
        leg = await adapter.dial(
            connection_id="app-1",
            to="+15559990000",
            from_number="+15550001234",
            link_to="v3:a",
            client_state="cw==",
            detect_machine=True,
        )
        assert leg == "v3:b"
        assert _sent(seen[0]) == (
            "/v2/calls",
            {
                "connection_id": "app-1",
                "to": "+15559990000",
                "from": "+15550001234",
                "link_to": "v3:a",
                "bridge_intent": True,
                "client_state": "cw==",
                "answering_machine_detection": "detect",
            },
        )

    async def test_no_machine_detection_unless_asked(self) -> None:
        adapter, seen = _adapter({"call_control_id": "v3:b"})
        await adapter.dial(
            connection_id="app-1",
            to="+1",
            from_number="+2",
            link_to="v3:a",
            client_state="cw==",
            detect_machine=False,
        )
        assert "answering_machine_detection" not in _sent(seen[0])[1]

    async def test_bridging_and_ringback(self) -> None:
        adapter, seen = _adapter()
        await adapter.bridge("v3:b", "v3:a", client_state="cw==")
        await adapter.start_playback("v3:a", b"RIFF")
        await adapter.stop_playback("v3:a")
        assert [_sent(r) for r in seen] == [
            (
                "/v2/calls/v3:b/actions/bridge",
                {"call_control_id": "v3:a", "client_state": "cw=="},
            ),
            (
                "/v2/calls/v3:a/actions/playback_start",
                {
                    "playback_content": "UklGRg==",
                    "audio_type": "wav",
                    "loop": "infinity",
                },
            ),
            ("/v2/calls/v3:a/actions/playback_stop", {"stop": "all"}),
        ]


class TestTheClientState:
    def test_it_names_the_call_and_the_step(self) -> None:
        call_id = uuid.uuid4()
        state = telnyx_transfer.read_client_state(
            {"client_state": telnyx_transfer.client_state(call_id, "operator")}
        )
        assert state is not None
        assert telnyx_transfer.call_id_of(state) == call_id
        assert state["step"] == "operator"

    @pytest.mark.parametrize(
        "raw", [None, "", "not base64!", "e30=", "eyJ0cmFuc2ZlciI6ICJ4In0="]
    )
    def test_anything_else_is_not_a_transfer(self, raw) -> None:
        # e30= is {}; the last names a call that is not a UUID.
        assert telnyx_transfer.read_client_state({"client_state": raw}) is None


def test_the_ringback_is_one_cadence_of_8khz_audio() -> None:
    with wave.open(io.BytesIO(telnyx_transfer.ringback_wav())) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            8000,
            1,
            2,
        )
        assert wav.getnframes() == 6 * 8000  # two seconds on, four off


def _call(**metadata: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        provider="telnyx",
        provider_call_sid="v3:a",
        status="in_progress",
        from_number="+15550001234",
        to_number="+15550005678",
        metadata_json=metadata,
    )


class TestAStartThatFails:
    async def test_without_a_connection_the_stream_is_left_alone(self) -> None:
        adapter = MagicMock()
        adapter.stop_streaming = AsyncMock()
        with pytest.raises(telnyx_transfer.TransferUnavailable):
            await telnyx_transfer.start(
                adapter, _call(), TransferIntent("+1", "cold"), detect_machine=False
            )
        adapter.stop_streaming.assert_not_awaited()

    async def test_after_the_stream_stopped_the_caller_is_not_left_in_silence(
        self,
    ) -> None:
        adapter = MagicMock(
            stop_streaming=AsyncMock(),
            start_playback=AsyncMock(),
            dial=AsyncMock(side_effect=RuntimeError("refused")),
            end_call=AsyncMock(),
        )
        call = _call(telnyx_connection_id="app-1")
        with pytest.raises(RuntimeError):
            await telnyx_transfer.start(
                adapter, call, TransferIntent("+1", "cold"), detect_machine=False
            )
        adapter.end_call.assert_awaited_once_with("v3:a")

    async def test_call_control_reports_it_and_forgets_the_intent(self) -> None:
        """The intent is what the media edge reads as "transferring, keep the
        call up"; one left behind would leave a silent line after the agent."""
        call = _call()
        account = MagicMock(spec=TelnyxAccount, api_key="KEY")
        account.adapter.return_value = MagicMock()
        store, clear = AsyncMock(), AsyncMock()
        with (
            patch(
                "turncall.storage.repositories.call_repo.get_call_by_id",
                AsyncMock(return_value=call),
            ),
            patch("turncall.storage.repositories.call_repo.update_call_status"),
            patch(
                "turncall.services.telnyx_accounts.account_for_project",
                AsyncMock(return_value=account),
            ),
            patch("turncall.services.transfer.store_transfer_intent", store),
            patch("turncall.services.transfer.clear_transfer_intent", clear),
            patch("turncall.events.emit.emit_call_event", AsyncMock()),
        ):
            result = await call_control.transfer_call(
                AsyncMock(), call.id, target_number="+15559990000"
            )
        assert not result.success
        store.assert_awaited_once()
        clear.assert_awaited_once_with(call.id)
