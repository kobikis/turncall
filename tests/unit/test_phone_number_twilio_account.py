"""A phone number bound with its own Twilio account.

The SID is looked up on that account (which is what proves the credentials own
the number), the token is sealed and never read back, and every later Twilio
call and signature check for that number uses that account.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from twilio.base.exceptions import TwilioRestException

from turncall.api.deps import get_session
from turncall.api.v1.schemas.phone_numbers import BindPhoneNumberRequest
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole
from turncall.services import twilio_accounts

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
SID = "AC" + "a" * 32
TOKEN = "own-twilio-auth-token"
ADAPTER = "turncall.adapters.telephony.twilio_adapter.TwilioAdapter"
REPO = "turncall.storage.repositories.phone_number_repo"


def _settings(key: str | None = None, platform: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        credentials=SimpleNamespace(
            encryption_key=key or Fernet.generate_key().decode()
        ),
        twilio=SimpleNamespace(
            account_sid="AC" + "p" * 32 if platform else "",
            auth_token="platform-token" if platform else "",
        ),
        server=SimpleNamespace(public_base_url="https://tc.example"),
    )


class TestSchema:
    def test_the_pair_is_both_or_neither(self) -> None:
        with pytest.raises(ValidationError, match="together"):
            BindPhoneNumberRequest(
                e164_number="+15551234567",
                external_number_sid="PN1",
                routing_target_type="agent",
                routing_target_id=uuid.uuid4(),
                twilio_account_sid=SID,
            )

    def test_the_number_sid_is_optional_only_with_an_own_account(self) -> None:
        base = {
            "e164_number": "+15551234567",
            "routing_target_type": "agent",
            "routing_target_id": uuid.uuid4(),
        }
        with pytest.raises(ValidationError, match="external_number_sid is required"):
            BindPhoneNumberRequest(**base)
        BindPhoneNumberRequest(**base, twilio_account_sid=SID, twilio_auth_token=TOKEN)

    def test_an_account_sid_must_look_like_one(self) -> None:
        with pytest.raises(ValidationError):
            BindPhoneNumberRequest(
                e164_number="+15551234567",
                routing_target_type="agent",
                routing_target_id=uuid.uuid4(),
                twilio_account_sid="PN123",
                twilio_auth_token=TOKEN,
            )


class TestResolution:
    def test_a_number_with_its_own_account_uses_it(self) -> None:
        s = _settings()
        row = SimpleNamespace(
            twilio_account_sid=SID,
            twilio_auth_token_ciphertext=twilio_accounts.encrypt_token(TOKEN, s),
        )
        assert twilio_accounts.account_for_number(
            row, s
        ) == twilio_accounts.TwilioAccount(SID, TOKEN)

    def test_a_number_without_one_uses_the_platforms(self) -> None:
        s = _settings()
        row = SimpleNamespace(
            twilio_account_sid=None, twilio_auth_token_ciphertext=None
        )
        assert twilio_accounts.account_for_number(row, s).auth_token == "platform-token"
        assert (
            twilio_accounts.account_for_number(row, _settings(platform=False)) is None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("direction", "looked_up"), [("inbound", "+1TO"), ("outbound", "+1FROM")]
    )
    async def test_a_call_resolves_through_turncalls_own_number(
        self, direction, looked_up
    ) -> None:
        call = SimpleNamespace(
            direction=direction, from_number="+1FROM", to_number="+1TO"
        )
        with patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)) as get:
            await twilio_accounts.account_for_call(AsyncMock(), call, _settings())
        assert get.await_args.args[1] == looked_up

    @pytest.mark.asyncio
    async def test_a_webhook_is_verified_with_the_sending_accounts_token(self) -> None:
        s = _settings()
        session = AsyncMock()
        session.scalar.return_value = twilio_accounts.encrypt_token(TOKEN, s)
        assert await twilio_accounts.token_for_account_sid(session, SID, s) == TOKEN

    @pytest.mark.asyncio
    async def test_an_unknown_account_still_needs_the_platform_signature(self) -> None:
        s = _settings()
        session = AsyncMock()
        session.scalar.return_value = None
        token = await twilio_accounts.token_for_account_sid(session, "AC" + "f" * 32, s)
        assert token == "platform-token"


@pytest.fixture
def project_id(app, monkeypatch):
    pid = uuid.uuid4()
    ctx = AuthContext(project_id=pid, api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN)
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    settings = _settings()
    monkeypatch.setattr("turncall.api.v1.phone_numbers.get_settings", lambda: settings)
    return pid


def _row(**kw):
    row = {
        "id": uuid.uuid4(),
        "provider": "twilio",
        "routing_weights": None,
        "server_url": None,
        "server_url_secret": None,
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
        **kw,
    }
    row["metadata_json"] = row.get("metadata_json") or {}
    return SimpleNamespace(**row)


BODY = {
    "e164_number": "+15551234567",
    "routing_target_type": "agent",
    "routing_target_id": str(uuid.uuid4()),
    "twilio_account_sid": SID,
    "twilio_auth_token": TOKEN,
}


class TestBind:
    def test_binds_with_the_looked_up_sid_and_seals_the_token(self, app, project_id):
        bind = AsyncMock(side_effect=lambda session, **kw: _row(**kw))
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{REPO}.bind_phone_number", bind),
            patch(f"{ADAPTER}.find_number_sid", AsyncMock(return_value="PNfound")),
            patch(f"{ADAPTER}.configure_number_webhook", AsyncMock()) as hook,
            patch(f"{ADAPTER}.configure_sms_webhook", AsyncMock()),
        ):
            r = app.post("/v1/phone-numbers", headers=H, json=BODY)

        assert r.status_code == 201, r.text
        kw = bind.await_args.kwargs
        assert kw["external_number_sid"] == "PNfound"
        assert kw["twilio_account_sid"] == SID
        assert TOKEN not in kw["twilio_auth_token_ciphertext"]
        data = r.json()["data"]
        assert data["twilio_credentials"] == "number"
        assert data["twilio_account_sid"] == SID
        assert TOKEN not in r.text
        assert hook.await_args.args[0] == "PNfound"  # configured on that account

    def test_rejected_credentials_bind_nothing(self, app, project_id):
        refused = TwilioRestException(401, "uri", msg="Authenticate")
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{REPO}.bind_phone_number", AsyncMock()) as bind,
            patch(f"{ADAPTER}.find_number_sid", AsyncMock(side_effect=refused)),
        ):
            r = app.post("/v1/phone-numbers", headers=H, json=BODY)
        assert r.status_code == 422
        assert "rejected" in r.json()["error"]
        bind.assert_not_awaited()

    def test_a_number_the_account_does_not_own_is_refused(self, app, project_id):
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{REPO}.bind_phone_number", AsyncMock()) as bind,
            patch(f"{ADAPTER}.find_number_sid", AsyncMock(return_value=None)),
        ):
            r = app.post("/v1/phone-numbers", headers=H, json=BODY)
        assert r.status_code == 422
        assert "+15551234567 is not a phone number" in r.json()["error"]
        bind.assert_not_awaited()
