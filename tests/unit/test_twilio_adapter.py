"""Tests for Twilio adapter (TwiML generation, signature validation)."""

import uuid
import xml.etree.ElementTree as ET

import pytest

from turncall.adapters.telephony.twilio_adapter import TwilioAdapter


@pytest.mark.unit
class TestTwiMLGeneration:
    def _make_adapter(self) -> TwilioAdapter:
        """Create adapter with dummy credentials (no API calls made)."""
        return TwilioAdapter("AC_test_sid", "test_auth_token")

    def test_media_stream_twiml_structure(self) -> None:
        adapter = self._make_adapter()
        call_id = uuid.uuid4()
        twiml = adapter.generate_media_stream_twiml(
            "wss://example.com/ws/media-stream", call_id
        )

        root = ET.fromstring(twiml)
        assert root.tag == "Response"

        connect = root.find("Connect")
        assert connect is not None

        stream = connect.find("Stream")
        assert stream is not None
        assert stream.get("url") == "wss://example.com/ws/media-stream"

        params = stream.findall("Parameter")
        param_dict = {p.get("name"): p.get("value") for p in params}
        assert param_dict["call_id"] == str(call_id)

    def test_transfer_twiml_basic(self) -> None:
        # Cold, no extras → bare <Dial>target</Dial>.
        twiml = TwilioAdapter.build_transfer_twiml("+15551234567")
        root = ET.fromstring(twiml)
        assert root.tag == "Response"

        dial = root.find("Dial")
        assert dial is not None
        assert dial.text == "+15551234567"
        assert dial.find("Number") is None

    def test_transfer_twiml_with_caller_message(self) -> None:
        twiml = TwilioAdapter.build_transfer_twiml(
            "+15551234567",
            transfer_message="Connecting you to support.",
        )
        root = ET.fromstring(twiml)

        say = root.find("Say")
        assert say is not None
        assert say.text == "Connecting you to support."
        assert root.find("Dial").text == "+15551234567"

    def test_transfer_twiml_warm_with_whisper_and_amd(self) -> None:
        twiml = TwilioAdapter.build_transfer_twiml(
            "+15551234567",
            whisper_url="https://x.test/whisper/abc",
            action_url="https://x.test/result/abc",
            amd_callback_url="https://x.test/amd/abc",
        )
        root = ET.fromstring(twiml)
        dial = root.find("Dial")
        assert dial.get("action") == "https://x.test/result/abc"

        number = dial.find("Number")
        assert number is not None
        assert number.text == "+15551234567"
        assert number.get("url") == "https://x.test/whisper/abc"
        assert number.get("machineDetection") == "Enable"
        assert number.get("amdStatusCallback") == "https://x.test/amd/abc"

    def test_signature_validation_rejects_invalid(self) -> None:
        adapter = self._make_adapter()
        result = adapter.validate_webhook_signature(
            signature="invalid-sig",
            url="https://example.com/webhook",
            params={"key": "value"},
        )
        assert result is False
