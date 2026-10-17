"""A WhatsApp webhook nothing can verify (#339).

Without WHATSAPP_APP_SECRET, production refuses every signed POST before
anything is read, development accepts it as it always has, the subscription
handshake (GET, hub.challenge) works either way, and startup says so when
WhatsApp is configured.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import JSONResponse
from loguru import logger

from turncall.config.settings import Environment, Settings, WhatsAppSettings
from turncall.storage.database import get_session
from turncall.webhooks import whatsapp_handlers

pytestmark = pytest.mark.unit

SECRET = "0123456789abcdef0123456789abcdef"

MESSAGE = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "metadata": {"display_phone_number": "+15551234567"},
                        "messages": [
                            {
                                "from": "15559876543",
                                "id": "wamid.1",
                                "type": "text",
                                "text": {"body": "hi"},
                            }
                        ],
                    },
                }
            ]
        }
    ],
}

CALL = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "changes": [
                {
                    "field": "calls",
                    "value": {"calls": [{"id": "wacid.1", "event": "connect"}]},
                }
            ]
        }
    ],
}


def _settings(
    *,
    production: bool,
    app_secret: str = "",
    token: str = "",
    phone_number_id: str = "",
    verify_token: str = "",
) -> Settings:
    return Settings().model_copy(
        update={
            "environment": (
                Environment.PRODUCTION if production else Environment.DEVELOPMENT
            ),
            "whatsapp": WhatsAppSettings(
                WHATSAPP_TOKEN=token,
                WHATSAPP_PHONE_NUMBER_ID=phone_number_id,
                WHATSAPP_APP_SECRET=app_secret,
                WHATSAPP_WEBHOOK_VERIFY_TOKEN=verify_token,
            ),
        }
    )


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def db_session(app):
    """The route's DbSession, a mock that records any use."""
    session = MagicMock()

    async def _session():
        yield session

    app.app.dependency_overrides[get_session] = _session
    yield session
    app.app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def handlers():
    """The two event handlers, standing in for everything a webhook does."""
    ok = JSONResponse({"status": "ok"})
    with (
        patch.object(
            whatsapp_handlers, "_handle_call_event", AsyncMock(return_value=ok)
        ) as call,
        patch.object(
            whatsapp_handlers, "_handle_message_event", AsyncMock(return_value=ok)
        ) as message,
    ):
        yield {"calls": call, "messages": message}


def _post(app, settings: Settings, payload: dict, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    with patch.object(whatsapp_handlers, "get_settings", return_value=settings):
        return app.post("/webhooks/whatsapp", content=body, headers=headers)


class TestProductionWithoutAppSecret:
    @pytest.mark.parametrize("payload", [MESSAGE, CALL], ids=["messages", "calls"])
    def test_refused_and_nothing_processed(
        self, app, db_session, handlers, payload
    ) -> None:
        body = json.dumps(payload).encode()
        # Even one signed with what would be the secret: nothing can check it.
        resp = _post(app, _settings(production=True), payload, _sign(body))

        assert resp.status_code == 403
        handlers["calls"].assert_not_awaited()
        handlers["messages"].assert_not_awaited()
        assert db_session.mock_calls == []

    def test_refusal_is_logged(self, app, db_session, handlers) -> None:
        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
        try:
            resp = _post(app, _settings(production=True), MESSAGE)
        finally:
            logger.remove(sink)

        assert resp.status_code == 403
        assert any(
            "whatsapp_webhook_refused" in line and "WHATSAPP_APP_SECRET" in line
            for line in seen
        )

    def test_subscription_handshake_still_works(self, app) -> None:
        settings = _settings(production=True, verify_token="my-token")
        with patch.object(whatsapp_handlers, "get_settings", return_value=settings):
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


class TestWithAppSecret:
    @pytest.mark.parametrize("production", [True, False], ids=["prod", "dev"])
    @pytest.mark.parametrize("payload", [MESSAGE, CALL], ids=["messages", "calls"])
    def test_valid_signature_is_accepted(
        self, app, db_session, handlers, production, payload
    ) -> None:
        body = json.dumps(payload).encode()
        settings = _settings(production=production, app_secret=SECRET)

        resp = _post(app, settings, payload, _sign(body))

        assert resp.status_code == 200
        field = payload["entry"][0]["changes"][0]["field"]
        handlers[field].assert_awaited_once()

    @pytest.mark.parametrize("signature", [None, "sha256=deadbeef"])
    def test_bad_signature_is_refused(
        self, app, db_session, handlers, signature
    ) -> None:
        settings = _settings(production=True, app_secret=SECRET)

        resp = _post(app, settings, MESSAGE, signature)

        assert resp.status_code == 403
        handlers["messages"].assert_not_awaited()


class TestDevelopmentWithoutAppSecret:
    def test_accepted_unverified_as_before(self, app, db_session, handlers) -> None:
        resp = _post(app, _settings(production=False), MESSAGE)

        assert resp.status_code == 200
        handlers["messages"].assert_awaited_once()


class TestStartupWarning:
    """WhatsApp is configured when WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID or
    WHATSAPP_WEBHOOK_VERIFY_TOKEN is set, or a number has whatsapp_enabled."""

    async def _warn(self, settings: Settings, *, numbers: bool = False) -> str | None:
        with patch.object(
            whatsapp_handlers.phone_number_repo,
            "any_whatsapp_enabled",
            AsyncMock(return_value=numbers),
        ):
            return await whatsapp_handlers.warn_if_unverifiable(settings, MagicMock())

    @pytest.mark.parametrize(
        ("setting", "name"),
        [
            ("token", "WHATSAPP_TOKEN"),
            ("phone_number_id", "WHATSAPP_PHONE_NUMBER_ID"),
            ("verify_token", "WHATSAPP_WEBHOOK_VERIFY_TOKEN"),
        ],
    )
    async def test_a_whatsapp_setting_without_the_secret_warns(
        self, setting, name
    ) -> None:
        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
        try:
            warning = await self._warn(_settings(production=True, **{setting: "x"}))
        finally:
            logger.remove(sink)

        assert warning is not None
        assert name in warning
        assert "WHATSAPP_APP_SECRET" in warning
        assert "refused" in warning
        assert any(warning in line for line in seen)

    async def test_a_whatsapp_number_without_the_secret_warns(self) -> None:
        warning = await self._warn(_settings(production=True), numbers=True)

        assert warning is not None
        assert "whatsapp_enabled" in warning

    async def test_development_says_it_accepts_unverified(self) -> None:
        warning = await self._warn(_settings(production=False, token="x"))

        assert warning is not None
        assert "accepted unverified" in warning

    async def test_with_the_secret_nothing_is_said(self) -> None:
        settings = _settings(production=True, app_secret=SECRET, token="x")

        assert await self._warn(settings, numbers=True) is None

    async def test_without_whatsapp_nothing_is_said(self) -> None:
        assert await self._warn(_settings(production=True)) is None
