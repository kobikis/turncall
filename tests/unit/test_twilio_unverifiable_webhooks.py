"""A Twilio webhook no token can verify, in every environment (#337).

With no `TWILIO_AUTH_TOKEN` (or one that is only whitespace) and no number on
its own Twilio account, every `/webhooks/twilio/*` route used to take an
unsigned request. Development keeps that; staging and production refuse it
with a 403 before the request touches anything, and say why in the log, at
most once a minute per route. Startup warns of the Twilio numbers in that
state, plainly when development is only TURNCALL_ENV's default.

`/webhooks/twilio/stream-status` is verified too, against the URL the TwiML
answering the call gave Twilio, built from the same origin.
"""

import uuid
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from starlette.requests import Request
from twilio.request_validator import RequestValidator

from turncall.config.settings import Environment, Settings, TwilioSettings
from turncall.services import twilio_accounts, webhook_verification
from turncall.storage.database import get_session
from turncall.webhooks import twilio_handlers as th

pytestmark = pytest.mark.unit

REPO = "turncall.storage.repositories.phone_number_repo"
CALL_ID = uuid.UUID(int=1)  # fixed: it names parametrized tests
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


@pytest.fixture(autouse=True)
def _fresh_refusal_log():
    """Each test sees its own first refusal, not one another test logged."""
    webhook_verification.refused_webhooks.reset()
    yield
    webhook_verification.refused_webhooks.reset()


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


class TestRefusedOutsideDevelopment:
    @pytest.mark.parametrize(
        "environment", [Environment.PRODUCTION, Environment.STAGING]
    )
    @pytest.mark.parametrize("route", ROUTES)
    def test_every_twilio_route_refuses_an_unverifiable_webhook(
        self, monkeypatch, stores, warnings, environment, route
    ) -> None:
        client, session = _client(monkeypatch, _settings(environment))

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

    def test_a_whitespace_token_is_no_token(self, monkeypatch, stores) -> None:
        # A template rendering the token as " ": a key anyone can sign with.
        settings = _settings(Environment.PRODUCTION, auth_token="  ")
        url = "http://testserver/webhooks/twilio/voice/inbound"
        forged = RequestValidator("  ").compute_signature(url, FORM)
        client, _ = _client(monkeypatch, settings)

        response = client.post(
            ROUTES[0], data=FORM, headers={"X-Twilio-Signature": forged}
        )

        assert settings.twilio.auth_token == ""
        assert response.status_code == 403
        assert stores["open_inbound_call"].await_count == 0

    def test_a_flood_of_refusals_is_one_line_per_route(
        self, monkeypatch, stores, warnings
    ) -> None:
        client, _ = _client(monkeypatch, _settings(Environment.PRODUCTION))
        # A forged AccountSid of any length reaches the log bounded.
        form = {**FORM, "AccountSid": "AC" + "9" * 5000}

        for _ in range(20):
            assert client.post(ROUTES[0], data=form).status_code == 403
        assert client.post(ROUTES[2], data=form).status_code == 403

        lines = [w for w in warnings if "twilio_webhook_unverifiable" in w]
        assert len(lines) == 2  # one per route, not one per request
        assert all(len(line) < 1000 for line in lines)


class TestRefusalLog:
    def test_says_how_many_it_left_out_once_the_interval_passes(self, warnings) -> None:
        now = [0.0]
        log = webhook_verification.RefusalLog(interval=60, clock=lambda: now[0])

        assert log.warn("/r", "refused {who}", who="a\nb")
        assert not log.warn("/r", "refused {who}", who="c")
        assert not log.warn("/r", "refused {who}", who="d")
        assert log.warn("/other", "refused {who}", who="e")
        now[0] = 61.0
        assert log.warn("/r", "refused {who}", who="f")

        assert len(warnings) == 3
        assert "refused ab" in warnings[0]  # no line break from the request
        assert "(2 more refused on this route since the last)" in warnings[2]


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


TOKEN = "f" * 32


def _inbound_request(headers: dict[str, str]) -> Request:
    """The voice webhook the TwiML answers, as the app sees it: plain http to
    an internal address, whatever the proxy in front of it did."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("10.0.0.5", 8090),
            "path": "/webhooks/twilio/voice/inbound",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


class TestStreamStatusSignature:
    """The statusCallback TwiML gives Twilio is the URL its signature is
    checked against, so a genuine stream-status callback is accepted."""

    @pytest.mark.parametrize(
        ("environment", "headers", "callback"),
        [
            # Production behind a TLS terminator that sends no
            # X-Forwarded-Proto: Twilio is told https, and checked as https.
            (
                Environment.PRODUCTION,
                {"host": "api.example.com"},
                "https://api.example.com/webhooks/twilio/stream-status",
            ),
            # A tunnel naming the public host and scheme.
            (
                Environment.DEVELOPMENT,
                {
                    "host": "turncall:8090",
                    "x-forwarded-proto": "https",
                    "x-forwarded-host": "abc.ngrok.io",
                },
                "https://abc.ngrok.io/webhooks/twilio/stream-status",
            ),
            # Nothing in front of it.
            (
                Environment.DEVELOPMENT,
                {"host": "localhost:8090"},
                "http://localhost:8090/webhooks/twilio/stream-status",
            ),
        ],
        ids=["production-tls-terminator", "tunnel", "direct"],
    )
    def test_the_callback_twilio_is_given_validates(
        self, monkeypatch, stores, environment, headers, callback
    ) -> None:
        settings = _settings(environment, auth_token=TOKEN)
        client, _ = _client(monkeypatch, settings)
        twiml = th.generate_media_stream_twiml(_inbound_request(headers), CALL_ID)
        stream = ET.fromstring(twiml).find(".//Stream")
        assert stream is not None
        assert stream.get("statusCallback") == callback
        scheme = "wss" if callback.startswith("https") else "ws"
        assert stream.get("url") == f"{scheme}://{urlsplit(callback).netloc}" + (
            "/ws/media-stream"
        )

        # Twilio signs the URL it was given, with the platform token.
        form = {
            "AccountSid": settings.twilio.account_sid,
            "CallSid": FORM["CallSid"],
            "StreamSid": FORM["StreamSid"],
            "StreamEvent": "stream-started",
        }
        signature = RequestValidator(TOKEN).compute_signature(callback, form)
        response = client.post(
            urlsplit(callback).path,
            data=form,
            headers={**headers, "X-Twilio-Signature": signature},
        )

        assert response.status_code == 204

    def test_a_forged_one_is_refused(self, monkeypatch, stores) -> None:
        settings = _settings(Environment.PRODUCTION, auth_token=TOKEN)
        client, _ = _client(monkeypatch, settings)
        callback = "https://api.example.com/webhooks/twilio/stream-status"
        signature = RequestValidator("not-the-token").compute_signature(callback, FORM)

        response = client.post(
            "/webhooks/twilio/stream-status",
            data=FORM,
            headers={"host": "api.example.com", "X-Twilio-Signature": signature},
        )

        assert response.status_code == 403


class TestStartupWarning:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("environment", "owned", "outcome"),
        [
            (Environment.PRODUCTION, False, "refused (403)"),
            (Environment.STAGING, False, "refused (403)"),
            (Environment.DEVELOPMENT, True, "refused (403)"),
            (
                Environment.DEVELOPMENT,
                False,
                "accepted unverified (TURNCALL_ENV=development)",
            ),
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
    async def test_default_development_says_turncall_env_is_unset(
        self, monkeypatch, warnings
    ) -> None:
        monkeypatch.delenv("TURNCALL_ENV", raising=False)
        monkeypatch.setattr(
            f"{REPO}.count_on_platform_twilio_account", AsyncMock(return_value=1)
        )
        monkeypatch.setattr(
            f"{REPO}.any_has_own_twilio_account", AsyncMock(return_value=False)
        )
        settings = Settings(_env_file=None)
        settings.twilio = TwilioSettings(TWILIO_AUTH_TOKEN="")
        assert settings.environment_is_default

        await twilio_accounts.warn_unverifiable_numbers(AsyncMock(), settings)

        [warning] = [w for w in warnings if "twilio_numbers_unverifiable" in w]
        assert "accepted unverified" in warning
        assert "TURNCALL_ENV is not set" in warning

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
            "turncall.storage.database.get_session_factory",
            lambda: (_ for _ in ()).throw(RuntimeError("no db")),
        )

        await turncall_app._warn_unverifiable_webhooks(Settings())

        # Each check fails on its own, and neither stops the other.
        for failed in (
            "whatsapp_verification_check_failed",
            "twilio_numbers_check_failed",
        ):
            assert any(failed in w for w in warnings)
