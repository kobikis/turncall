"""The carrier boundary (#201, ADR-0022).

`TelephonyAdapter` is a carrier's REST actions and nothing else; the media
entry point picks the serializer from the call's own carrier; the API calls a
number's carrier `carrier`, still taking `provider` for it. The end-to-end half
is `tests/integration/test_carrier_contract.py`.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from turncall.adapters.telephony.base import TelephonyAdapter
from turncall.api.deps import get_session
from turncall.api.v1.schemas.phone_numbers import (
    BindPhoneNumberRequest,
    PhoneNumberResponse,
    UpdatePhoneNumberRequest,
)
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.carriers import CARRIERS
from turncall.domain.enums import ProjectRole
from turncall.orchestrator import transport_factory as tf
from turncall.orchestrator.serializer import TwilioFrameSerializer
from turncall.services.media_token import media_token
from turncall.webhooks import media_stream

pytestmark = pytest.mark.unit

# Every carrier has these; nothing about how one signs a webhook or answers.
REST_ACTIONS = {
    "initiate_outbound_call",
    "end_call",
    "transfer_call",
    "send_dtmf",
    "get_recording_url",
    "configure_number_webhook",
    "fetch_number_info",
    "send_sms",
    "configure_sms_webhook",
}


class TestTheAdapterIsRestOnly:
    def test_it_declares_rest_actions_only(self) -> None:
        """A new abstract method has to be one every carrier can answer.
        TwiML and Twilio's HMAC signature check are its edge's, not here."""
        assert set(TelephonyAdapter.__abstractmethods__) == REST_ACTIONS


class TestEveryCarrierHasAMediaStream:
    def test_each_carrier_has_a_serializer_and_a_rendered_rate(self) -> None:
        assert set(tf.CARRIER_MEDIA) == set(CARRIERS)
        for media in tf.CARRIER_MEDIA.values():
            assert media.sample_rate in tf.VOICE_OUTPUT_RATES

    def test_twilio_gets_twilios_serializer_for_its_stream(self) -> None:
        transport = tf.create_carrier_transport(
            "twilio", MagicMock(), tf.CarrierStream("MZ42", "CA42")
        )
        serializer = transport._params.serializer
        assert isinstance(serializer, TwilioFrameSerializer)
        assert serializer._stream_sid == "MZ42"
        assert transport._params.audio_in_sample_rate == tf.TWILIO_SAMPLE_RATE


class _Session:
    async def __aenter__(self):
        return AsyncMock()

    async def __aexit__(self, *exc):
        return False


async def _run_media_stream(carrier: str) -> MagicMock:
    """Hand a stream to the shared entry point for a call on `carrier`;
    returns the transport factory, to see what it was asked for."""
    call_row = SimpleNamespace(
        provider=carrier,
        project_id=uuid.uuid4(),
        active_agent_id=uuid.uuid4(),
        metadata_json={},
    )
    agent = SimpleNamespace(config_blob={"system_prompt": "hi"})
    create = MagicMock(return_value="transport")
    session = SimpleNamespace(start=AsyncMock())
    with (
        patch.object(media_stream, "get_engine"),
        patch.object(media_stream, "create_session_factory", lambda _e: _Session),
        patch(
            "turncall.storage.repositories.call_repo.get_call_by_id",
            AsyncMock(return_value=call_row),
        ),
        patch(
            "turncall.storage.repositories.call_repo.attach_media",
            AsyncMock(return_value=True),
        ),
        patch(
            "turncall.storage.repositories.agent_repo.get_agent_by_id",
            AsyncMock(return_value=agent),
        ),
        patch.object(media_stream, "create_carrier_transport", create),
        patch.object(
            media_stream, "build_call_pipeline", AsyncMock(return_value=session)
        ),
    ):
        call_id = uuid.uuid4()
        await media_stream.run_media_stream(
            MagicMock(),
            call_id,
            tf.CarrierStream("MZ1", "CA1"),
            carrier=carrier,
            token=media_token(call_id),
        )
    return create


class TestTheMediaEntryPoint:
    async def test_it_picks_the_serializer_from_the_calls_carrier(self) -> None:
        create = await _run_media_stream("twilio")
        assert create.call_args.args[0] == "twilio"
        assert create.call_args.args[2] == tf.CarrierStream("MZ1", "CA1")

    async def test_a_carrier_with_no_media_stream_builds_nothing(self) -> None:
        """Decoding one carrier's audio as another's is noise, not a call."""
        create = await _run_media_stream("no-such-carrier")
        create.assert_not_called()


REQUIRED = {
    "external_number_sid": "PN123",
    "e164_number": "+15551234567",
    "routing_target_type": "agent",
    "routing_target_id": str(uuid.uuid4()),
}


class TestCarrierOnWrites:
    def test_absent_is_twilio(self) -> None:
        assert BindPhoneNumberRequest(**REQUIRED).carrier == "twilio"

    @pytest.mark.parametrize("key", ["carrier", "provider"])
    def test_carrier_and_its_deprecated_alias(self, key) -> None:
        assert BindPhoneNumberRequest(**REQUIRED, **{key: "twilio"}).carrier == "twilio"

    @pytest.mark.parametrize("key", ["carrier", "provider"])
    def test_an_unknown_carrier_is_refused(self, key) -> None:
        with pytest.raises(ValidationError):
            BindPhoneNumberRequest(**REQUIRED, **{key: "deepgram"})

    def test_the_two_spellings_must_agree(self) -> None:
        with pytest.raises(ValidationError, match="disagree"):
            BindPhoneNumberRequest.model_validate(
                {**REQUIRED, "carrier": "twilio", "provider": "telnyx"}
            )

    def test_an_update_leaves_it_alone_unless_sent(self) -> None:
        body = {"routing_target_type": "agent", "routing_target_id": uuid.uuid4()}
        assert UpdatePhoneNumberRequest(**body).carrier is None
        assert UpdatePhoneNumberRequest(**body, provider="twilio").carrier == "twilio"


def _row(**kw):
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "project_id": uuid.uuid4(),
            "provider": "twilio",
            "external_number_sid": "PN123",
            "e164_number": "+15551234567",
            "routing_target_type": "agent",
            "routing_target_id": uuid.uuid4(),
            "server_url": None,
            "server_url_secret": None,
            "sms_enabled": False,
            "routing_weights": None,
            "metadata_json": {},
            "created_at": datetime(2026, 10, 9, tzinfo=UTC),
            **kw,
        }
    )


def test_a_read_carries_carrier_and_the_deprecated_provider() -> None:
    data = PhoneNumberResponse.from_row(_row()).model_dump()
    assert data["carrier"] == "twilio"
    assert data["provider"] == "twilio"


REPO = "turncall.storage.repositories.phone_number_repo"
H = {"Authorization": "Bearer x"}


@pytest.fixture
def api(app):
    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    with patch(
        "turncall.api.v1.phone_numbers._configure_twilio_webhooks",
        AsyncMock(return_value=True),
    ):
        yield app


class TestCarrierThroughTheApi:
    def test_a_bind_by_the_old_name_stores_and_reads_back_carrier(self, api):
        bind = AsyncMock(side_effect=lambda session, **kw: _row(provider=kw["carrier"]))
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{REPO}.bind_phone_number", bind),
        ):
            r = api.post(
                "/v1/phone-numbers", headers=H, json={**REQUIRED, "provider": "twilio"}
            )
        assert r.status_code == 201, r.text
        assert bind.await_args.kwargs["carrier"] == "twilio"
        assert r.json()["data"]["carrier"] == "twilio"

    def test_a_number_cannot_change_carrier_in_place(self, api):
        """Its SID, webhooks and calls all live on the carrier it was bound on."""
        row = _row(provider="telnyx")
        update = AsyncMock()
        with (
            patch(f"{REPO}.get_by_id", AsyncMock(return_value=row)),
            patch(f"{REPO}.update_phone_number", update),
        ):
            r = api.put(
                f"/v1/phone-numbers/{row.id}",
                headers=H,
                json={
                    "carrier": "twilio",
                    "routing_target_type": "agent",
                    "routing_target_id": str(uuid.uuid4()),
                },
            )
        assert r.status_code == 422, r.text
        assert "telnyx" in r.json()["error"]
        update.assert_not_awaited()
