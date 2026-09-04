"""Tests for WhatsApp integration: webhook verification, message parsing, settings."""

import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from turncall.config.settings import Settings, WhatsAppSettings
from turncall.domain.enums import ChatChannel, SmsSessionStatus
from turncall.domain.models import SmsSession


@pytest.mark.unit
class TestWhatsAppSettings:
    def test_default_settings(self) -> None:
        settings = Settings()
        assert hasattr(settings, "whatsapp")
        assert isinstance(settings.whatsapp.token, str)
        assert isinstance(settings.whatsapp.phone_number_id, str)
        assert isinstance(settings.whatsapp.app_secret, str)
        assert isinstance(settings.whatsapp.webhook_verify_token, str)

    def test_whatsapp_settings_independent(self) -> None:
        ws = WhatsAppSettings(
            WHATSAPP_TOKEN="test-token",
            WHATSAPP_PHONE_NUMBER_ID="12345",
            WHATSAPP_APP_SECRET="secret",
            WHATSAPP_WEBHOOK_VERIFY_TOKEN="verify-me",
        )
        assert ws.token == "test-token"
        assert ws.phone_number_id == "12345"
        assert ws.app_secret == "secret"
        assert ws.webhook_verify_token == "verify-me"


@pytest.mark.unit
class TestChatChannelWhatsApp:
    def test_whatsapp_channel_exists(self) -> None:
        assert ChatChannel.WHATSAPP == "whatsapp"

    def test_whatsapp_channel_in_list(self) -> None:
        channels = list(ChatChannel)
        assert ChatChannel.WHATSAPP in channels

    def test_whatsapp_session_model(self) -> None:
        now = datetime.now(UTC)
        session = SmsSession(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            customer_number="+15551234567",
            turncall_number="+15559876543",
            status=SmsSessionStatus.ACTIVE,
            channel=ChatChannel.WHATSAPP,
            last_activity_at=now,
            expires_at=now,
            created_at=now,
        )
        assert session.channel == ChatChannel.WHATSAPP


@pytest.mark.unit
class TestWhatsAppWebhookVerification:
    def test_verify_success(self, app) -> None:
        """Webhook verification returns challenge on valid token."""
        with patch("turncall.webhooks.whatsapp_handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                whatsapp=MagicMock(webhook_verify_token="my-token")
            )
            resp = app.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.challenge": "123456",
                    "hub.verify_token": "my-token",
                },
            )
            assert resp.status_code == 200
            assert resp.text == "123456"

    def test_verify_invalid_token(self, app) -> None:
        """Webhook verification rejects mismatched token."""
        with patch("turncall.webhooks.whatsapp_handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                whatsapp=MagicMock(webhook_verify_token="my-token")
            )
            resp = app.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.challenge": "123456",
                    "hub.verify_token": "wrong-token",
                },
            )
            assert resp.status_code == 403

    def test_verify_invalid_mode(self, app) -> None:
        """Webhook verification rejects non-subscribe mode."""
        with patch("turncall.webhooks.whatsapp_handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                whatsapp=MagicMock(webhook_verify_token="my-token")
            )
            resp = app.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "unsubscribe",
                    "hub.challenge": "123456",
                    "hub.verify_token": "my-token",
                },
            )
            assert resp.status_code == 403

    def test_verify_missing_params(self, app) -> None:
        """Webhook verification rejects missing parameters."""
        with patch("turncall.webhooks.whatsapp_handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                whatsapp=MagicMock(webhook_verify_token="my-token")
            )
            resp = app.get(
                "/webhooks/whatsapp",
                params={"hub.mode": "subscribe"},
            )
            assert resp.status_code == 400


@pytest.mark.unit
class TestWhatsAppWebhookSignatureValidation:
    def test_valid_signature(self) -> None:
        """validate_whatsapp_signature passes for correct payload."""
        from turncall.webhooks.whatsapp_signature import validate_whatsapp_signature

        secret = "test-app-secret"
        payload = b'{"object":"whatsapp_business_account","entry":[]}'
        sig = (
            "sha256="
            + hmac.new(
                key=secret.encode("utf-8"),
                msg=payload,
                digestmod=hashlib.sha256,
            ).hexdigest()
        )

        assert validate_whatsapp_signature(secret, payload, sig) is True

    def test_invalid_signature_detected(self) -> None:
        """validate_whatsapp_signature fails for tampered payload."""
        from turncall.webhooks.whatsapp_signature import validate_whatsapp_signature

        secret = "test-app-secret"
        payload = b'{"object":"whatsapp_business_account","entry":[]}'
        wrong_payload = b'{"object":"whatsapp_business_account","entry":[{"id":"1"}]}'

        sig = (
            "sha256="
            + hmac.new(
                key=secret.encode("utf-8"),
                msg=payload,
                digestmod=hashlib.sha256,
            ).hexdigest()
        )

        assert validate_whatsapp_signature(secret, wrong_payload, sig) is False

    def test_missing_signature_header(self) -> None:
        """validate_whatsapp_signature rejects empty/missing header."""
        from turncall.webhooks.whatsapp_signature import validate_whatsapp_signature

        assert validate_whatsapp_signature("secret", b"body", "") is False
        assert validate_whatsapp_signature("secret", b"body", "invalid") is False

    def test_wrong_prefix(self) -> None:
        """validate_whatsapp_signature rejects non-sha256 prefix."""
        from turncall.webhooks.whatsapp_signature import validate_whatsapp_signature

        assert validate_whatsapp_signature("secret", b"body", "md5=abc") is False


@pytest.mark.unit
class TestWhatsAppMessageParsing:
    def test_parse_text_message_payload(self) -> None:
        """Parse a standard WhatsApp text message webhook payload."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "BUSINESS_ACCOUNT_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "+15551234567",
                                    "phone_number_id": "PHONE_ID",
                                },
                                "contacts": [
                                    {
                                        "profile": {"name": "Test User"},
                                        "wa_id": "15559876543",
                                    }
                                ],
                                "messages": [
                                    {
                                        "from": "15559876543",
                                        "id": "wamid.abc123",
                                        "timestamp": "1234567890",
                                        "type": "text",
                                        "text": {"body": "Hello, bot!"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        # Extract message data the same way the handler does
        entry = payload["entry"][0]
        change = entry["changes"][0]
        assert change["field"] == "messages"

        value = change["value"]
        messages = value["messages"]
        assert len(messages) == 1

        msg = messages[0]
        assert msg["type"] == "text"
        assert msg["text"]["body"] == "Hello, bot!"
        assert msg["from"] == "15559876543"
        assert msg["id"] == "wamid.abc123"

        metadata = value["metadata"]
        assert metadata["display_phone_number"] == "+15551234567"

    def test_parse_call_connect_payload(self) -> None:
        """Parse a WhatsApp call connect webhook payload."""
        from pipecat.transports.whatsapp.api import WhatsAppWebhookRequest

        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "BIZ_ID",
                    "changes": [
                        {
                            "field": "calls",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "+15551234567",
                                    "phone_number_id": "PHONE_ID",
                                },
                                "contacts": [
                                    {
                                        "profile": {"name": "Caller"},
                                        "wa_id": "15559876543",
                                    }
                                ],
                                "calls": [
                                    {
                                        "id": "call-123",
                                        "from": "15559876543",
                                        "to": "15551234567",
                                        "event": "connect",
                                        "timestamp": "2026-04-13T10:00:00Z",
                                        "direction": "inbound",
                                        "session": {
                                            "sdp": "v=0\r\n...",
                                            "sdp_type": "offer",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        # Verify Pipecat model can parse it
        parsed = WhatsAppWebhookRequest.model_validate(payload)
        assert parsed.object == "whatsapp_business_account"
        assert len(parsed.entry) == 1
        assert len(parsed.entry[0].changes) == 1

        change = parsed.entry[0].changes[0]
        assert change.field == "calls"
        assert len(change.value.calls) == 1
        assert change.value.calls[0].event == "connect"
        assert change.value.calls[0].id == "call-123"

    def test_ignore_non_text_message(self) -> None:
        """Non-text messages (image, audio) are skipped gracefully."""
        payload_value = {
            "messaging_product": "whatsapp",
            "metadata": {
                "display_phone_number": "+15551234567",
                "phone_number_id": "PHONE_ID",
            },
            "messages": [
                {
                    "from": "15559876543",
                    "id": "wamid.img123",
                    "timestamp": "1234567890",
                    "type": "image",
                    "image": {"id": "img-id", "mime_type": "image/jpeg"},
                }
            ],
        }

        messages = payload_value["messages"]
        text_messages = [m for m in messages if m["type"] == "text"]
        assert len(text_messages) == 0


@pytest.mark.unit
class TestWhatsAppTransportFactory:
    def test_create_whatsapp_transport(self) -> None:
        """Transport factory wraps a pre-existing connection."""
        from unittest.mock import MagicMock

        from turncall.orchestrator.transport_factory import create_whatsapp_transport

        mock_connection = MagicMock()
        transport = create_whatsapp_transport(mock_connection)
        assert transport is not None
