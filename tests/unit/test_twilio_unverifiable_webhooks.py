"""A Twilio webhook no token can verify, in production and in development (#337).

With no `TWILIO_AUTH_TOKEN` and no number on its own Twilio account, every
`/webhooks/twilio/*` route used to take an unsigned request. Development keeps
that; production refuses it with a 403 before the request touches anything,
and says why in the log. Startup warns of the Twilio numbers in that state.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from turncall.config.settings import Environment, Settings, TwilioSettings
from turncall.services import twilio_accounts
from turncall.storage.database import get_session
from turncall.webhooks import twilio_handlers as th

pytestmark = pytest.mark.unit

REPO = "turncall.storage.repositories.phone_number_repo"
CALL_ID = uuid.uuid4()
ROUTES = [
    "/webhooks/twilio/voice/inbound",
    "/webhooks/twilio/voice/outbound",
    "/webhooks/twilio/status",
    "/webhooks/twilio/recording",
    "/webhooks/twilio/stream-status",
    "/webhooks/twilio/sms/inbound",
    f"/webhooks/twilio/transfer-whisper/{CALL_ID}",
    f"/webhooks/twilio/transfer-result/{CALL_ID}",
    f"/webhooks/twilio/transfer-amd/{CALL_ID}",
]
# Every field any of the routes requires, as Twilio would post them.
FORM = {
    "AccountSid": "AC" + "1" * 32,
    "CallSid": "CA" + "2" * 32,
    "MessageSid": "SM" + "3" * 32,
    "From": "+15550001111",
    "To": "+15550002222",
    "Body": "hello",
    "CallStatus": "completed",
    "RecordingSid": "RE" + "4" * 32,
    "RecordingUrl": "https://api.twilio.com/recording",
    "RecordingStatus": "completed",
    "StreamSid": "MZ" + "5" * 32,
    "StreamEvent": "stream-started",
    "DialCallStatus": "no-answer",
    "AnsweredBy": "human",
}


def _settings(environment: Environment, *, auth_token: str = "") -> Settings:
    settings = Settings()
    settings.environment = environment
    settings.twilio = TwilioSettings(
        TWILIO_ACCOUNT_SID="AC" + "0" * 32, TWILIO_AUTH_TOKEN=auth_token
    )
    return settings


@pytest.fixture
def warnings():
    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    yield seen
    logger.remove(sink)


@pytest.fixture
def stores(monkeypatch):
    """No token anywhere: no number carries its own account. Everything a
    webhook would read or write next is a mock, to prove it never ran."""
    monkeypatch.setattr(
        f"{REPO}.token_ciphertext_for_account", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        f"{REPO}.any_has_own_twilio_account", AsyncMock(return_value=False)
    )
    work = {
        "get_by_e164": AsyncMock(return_value=None),
        "open_inbound_call": AsyncMock(),
        "handle_inbound_sms": AsyncMock(),
        "get_call_by_provider_sid": AsyncMock(return_value=None),
        "get_call_by_id": AsyncMock(return_value=None),
        "load_transfer_intent": AsyncMock(return_value=None),
    }
    monkeypatch.setattr(f"{REPO}.get_by_e164", work["get_by_e164"])
    monkeypatch.setattr(
        "turncall.services.inbound_call.open_inbound_call",
        work["open_inbound_call"],
    )
    monkeypatch.setattr(
        "turncall.services.sms_chat.handle_inbound_sms", work["handle_inbound_sms"]
    )
    for name in ("get_call_by_provider_sid", "get_call_by_id"):
        monkeypatch.setattr(
            f"turncall.storage.repositories.call_repo.{name}", work[name]
        )
    monkeypatch.setattr(
        "turncall.services.transfer.load_transfer_intent",
        work["load_transfer_intent"],
    )
    return work


def _client(monkeypatch, settings: Settings) -> tuple[TestClient, AsyncMock]:
    monkeypatch.setattr(th, "get_settings", lambda: settings)
    session = AsyncMock()
    app = FastAPI()
    app.include_router(th.router)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app), session


class TestProduction:
    @pytest.mark.parametrize("route", ROUTES)
    def test_every_twilio_route_refuses_an_unverifiable_webhook(
        self, monkeypatch, stores, warnings, route
    ) -> None:
        client, session = _client(monkeypatch, _settings(Environment.PRODUCTION))

        response = client.post(route, data=FORM)

        assert response.status_code == 403
        # Nothing read, nothing created: the session was never touched.
        assert session.mock_calls == []
        for name, mock in stores.items():
            assert not mock.await_count, f"{name} ran on a refused webhook"
        assert any(
            "twilio_webhook_unverifiable" in w and FORM["AccountSid"] in w
            for w in warnings
        )

    def test_unsigned_is_still_refused_with_a_token(self, monkeypatch, stores) -> None:
        settings = _settings(Environment.PRODUCTION, auth_token="secret")
        client, _ = _client(monkeypatch, settings)

        assert client.post(ROUTES[0], data=FORM).status_code == 403
        assert stores["open_inbound_call"].await_count == 0


class TestDevelopment:
    def test_voice_and_sms_pass_unverified_as_before(
        self, monkeypatch, stores, warnings
    ) -> None:
        client, _ = _client(monkeypatch, _settings(Environment.DEVELOPMENT))

        voice = client.post("/webhooks/twilio/voice/inbound", data=FORM)
        sms = client.post("/webhooks/twilio/sms/inbound", data=FORM)

        # Through to number lookup: the unknown-number answers, not a 403.
        assert voice.status_code == 200
        assert "not configured" in voice.text
        assert sms.status_code == 200
        assert "not configured for SMS" in sms.text
        assert stores["get_by_e164"].await_count == 2
        assert not any("twilio_webhook_unverifiable" in w for w in warnings)

    def test_staging_is_not_production(self, monkeypatch, stores) -> None:
        client, _ = _client(monkeypatch, _settings(Environment.STAGING))

        assert client.post(ROUTES[0], data=FORM).status_code == 200


class TestStartupWarning:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("environment", "owned", "outcome"),
        [
            (Environment.PRODUCTION, False, "refused (403)"),
            (Environment.DEVELOPMENT, True, "refused (403)"),
            (Environment.DEVELOPMENT, False, "accepted unverified"),
        ],
    )
    async def test_numbers_no_token_can_verify_are_named(
        self, monkeypatch, warnings, environment, owned, outcome
    ) -> None:
        monkeypatch.setattr(
            f"{REPO}.count_on_platform_twilio_account", AsyncMock(return_value=3)
        )
        monkeypatch.setattr(
            f"{REPO}.any_has_own_twilio_account", AsyncMock(return_value=owned)
        )

        count = await twilio_accounts.warn_unverifiable_numbers(
            AsyncMock(), _settings(environment)
        )

        assert count == 3
        [warning] = [w for w in warnings if "twilio_numbers_unverifiable" in w]
        assert "3 Twilio number(s)" in warning
        assert outcome in warning

    @pytest.mark.asyncio
    async def test_silent_with_a_platform_token_or_no_platform_numbers(
        self, monkeypatch, warnings
    ) -> None:
        counted = AsyncMock(return_value=0)
        monkeypatch.setattr(f"{REPO}.count_on_platform_twilio_account", counted)

        with_token = _settings(Environment.PRODUCTION, auth_token="secret")
        assert (
            await twilio_accounts.warn_unverifiable_numbers(AsyncMock(), with_token)
            == 0
        )
        assert counted.await_count == 0  # a token verifies them all: not asked
        no_token = _settings(Environment.PRODUCTION)
        assert (
            await twilio_accounts.warn_unverifiable_numbers(AsyncMock(), no_token) == 0
        )
        assert not any("twilio_numbers_unverifiable" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_failed_check_never_stops_startup(
        self, monkeypatch, warnings
    ) -> None:
        from turncall import app as turncall_app

        monkeypatch.setattr(
            "turncall.storage.database.get_engine",
            lambda: (_ for _ in ()).throw(RuntimeError("no db")),
        )

        await turncall_app._warn_unverifiable_webhooks(Settings())

        # Each check fails on its own, and neither stops the other.
        for failed in (
            "whatsapp_verification_check_failed",
            "twilio_numbers_check_failed",
        ):
            assert any(failed in w for w in warnings)
