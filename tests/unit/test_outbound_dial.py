"""Every outbound call goes through one dial service (#213).

The concurrency limit, the calls-per-second limit, the suppression list and
campaigns all hook in there, so a second dial path would bypass every one of
them silently. The route tests pin `POST /v1/calls/outbound` as it behaved
before the move; the guard fails on a new path.
"""

import ast
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import turncall
from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import CallEventType, ProjectRole
from turncall.services.limits import ProjectLimits
from turncall.services.media_token import media_token

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
ADAPTER = "turncall.adapters.telephony.twilio_adapter.TwilioAdapter"
TELNYX_ADAPTER = "turncall.adapters.telephony.telnyx_adapter.TelnyxAdapter"
TELNYX_ACCOUNT = "turncall.services.outbound_dial.telnyx_account_for_project"
CALLS = "turncall.storage.repositories.call_repo"
AGENTS = "turncall.storage.repositories.agent_repo"
NUMBERS = "turncall.storage.repositories.phone_number_repo"
SUPPRESSION = "turncall.storage.repositories.suppression_repo"
SRC = Path(turncall.__file__).parent

# The one module allowed to dial, and where carrier SDK dials may live.
DIAL_SERVICE = "services/outbound_dial.py"
CARRIER_ADAPTERS = "adapters/telephony/"


def _settings(base_url: str = "https://tc.example", platform: bool = True):
    return SimpleNamespace(
        twilio=SimpleNamespace(
            account_sid="AC" + "p" * 32 if platform else "",
            auth_token="platform-token" if platform else "",
        ),
        server=SimpleNamespace(public_base_url=base_url),
        credentials=SimpleNamespace(encryption_key=""),
    )


def _call(**kw):
    row = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "provider": "twilio",
        "direction": "outbound",
        "status": "queued",
        "from_number": "+15550000001",
        "to_number": "+15550000002",
        "active_agent_id": uuid.uuid4(),
        "provider_call_sid": None,
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "recording_status": "none",
        "recording_url": None,
        "metadata_json": {},
        "analysis_json": None,
        "created_at": datetime(2026, 10, 2, tzinfo=UTC),
        **kw,
    }
    return SimpleNamespace(**row)


@pytest.fixture
def dial(app, monkeypatch):
    """The route with every repository and the carrier mocked."""
    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    state = SimpleNamespace(settings=_settings(), project_id=ctx.project_id)
    monkeypatch.setattr(
        "turncall.services.outbound_dial.get_settings", lambda: state.settings
    )
    agent = SimpleNamespace(id=uuid.uuid4())
    number = SimpleNamespace(
        id=uuid.uuid4(),
        e164_number="+15550000001",
        provider="twilio",
        twilio_account_sid=None,
        twilio_auth_token_ciphertext=None,
    )
    created = _call(active_agent_id=agent.id)
    state.agent, state.number, state.created = agent, number, created
    state.suppressed = False
    state.body = {
        "agent_id": str(agent.id),
        "from_number_id": str(number.id),
        "to_number": "+15550000002",
        "metadata": {"crm": "C-1"},
    }
    with (
        patch(
            f"{AGENTS}.get_agent_by_id",
            AsyncMock(side_effect=lambda *a, **k: state.agent),
        ),
        patch(
            f"{NUMBERS}.get_by_id", AsyncMock(side_effect=lambda *a, **k: state.number)
        ),
        patch(
            f"{SUPPRESSION}.is_suppressed",
            AsyncMock(side_effect=lambda *a, **k: state.suppressed),
        ) as suppression,
        patch(f"{CALLS}.create_call", AsyncMock(return_value=created)) as create,
        patch(
            f"{CALLS}.update_call_status",
            AsyncMock(
                side_effect=lambda s, cid, **kw: _call(
                    id=cid,
                    **{
                        "status": kw["status"],
                        "provider_call_sid": kw["provider_call_sid"],
                    },
                )
            ),
        ) as update,
        patch(f"{CALLS}.get_next_sequence_number", AsyncMock(return_value=1)),
        # No limits here: those are tests/integration/test_limits.py's.
        patch(
            "turncall.services.limits.limits_for_project",
            AsyncMock(return_value=ProjectLimits()),
        ),
        patch(f"{CALLS}.create_call_event", AsyncMock()) as event,
        patch(
            f"{ADAPTER}.initiate_outbound_call", AsyncMock(return_value="CA123")
        ) as carrier,
    ):
        state.create, state.update, state.event, state.carrier = (
            create,
            update,
            event,
            carrier,
        )
        state.suppression = suppression
        state.post = lambda: app.post("/v1/calls/outbound", headers=H, json=state.body)
        yield state


class TestRoute:
    def test_dials_and_records_the_call(self, dial) -> None:
        r = dial.post()

        assert r.status_code == 201, r.text
        data = r.json()["data"]
        assert data["provider_call_sid"] == "CA123"
        assert data["status"] == "initiated"

        kw = dial.create.await_args.kwargs
        assert kw["direction"] == "outbound"
        assert kw["from_number"] == "+15550000001"
        assert kw["to_number"] == "+15550000002"
        assert kw["active_agent_id"] == dial.agent.id
        assert kw["metadata_json"] == {"crm": "C-1"}

        assert dial.carrier.await_args.kwargs == {
            "from_number": "+15550000001",
            "to_number": "+15550000002",
            "webhook_url": "https://tc.example/webhooks/twilio/voice/outbound",
            "status_callback_url": "https://tc.example/webhooks/twilio/status",
        }
        event = dial.event.await_args.kwargs
        assert event["event_type"] == CallEventType.CALL_STARTED
        assert event["payload"]["provider_call_sid"] == "CA123"
        assert event["payload"]["direction"] == "outbound"

    def test_unknown_agent_is_404(self, dial) -> None:
        dial.agent = None
        r = dial.post()
        assert r.status_code == 404
        dial.carrier.assert_not_awaited()

    def test_unknown_number_is_404(self, dial) -> None:
        dial.number = None
        r = dial.post()
        assert r.status_code == 404
        dial.carrier.assert_not_awaited()

    def test_no_twilio_account_is_503(self, dial) -> None:
        dial.settings = _settings(platform=False)
        r = dial.post()
        assert r.status_code == 503
        assert r.json()["code"] == "no_twilio_account"
        dial.carrier.assert_not_awaited()

    def test_a_telnyx_number_dials_on_telnyx(self, dial) -> None:
        """Same request, same answer; the dial goes to Telnyx, with the call's
        media stream named up front (#218)."""
        from turncall.services.telnyx_accounts import TelnyxAccount

        dial.number.provider = "telnyx"
        telnyx = AsyncMock(return_value="v3:abc")
        with (
            patch(
                f"{TELNYX_ACCOUNT}",
                AsyncMock(return_value=TelnyxAccount("KEY", "PUB")),
            ),
            patch(f"{TELNYX_ADAPTER}.connection_for", AsyncMock(return_value="app-1")),
            patch(f"{TELNYX_ADAPTER}.initiate_outbound_call", telnyx),
        ):
            r = dial.post()

        assert r.status_code == 201, r.text
        assert r.json()["data"]["provider_call_sid"] == "v3:abc"
        created = dial.create.await_args.kwargs
        assert created["carrier"] == "telnyx"
        # A transfer on this call dials the operator on the same application.
        assert created["metadata_json"] == {
            "crm": "C-1",
            "telnyx_connection_id": "app-1",
        }
        call_id = dial.created.id
        assert telnyx.await_args.kwargs == {
            "from_number": "+15550000001",
            "to_number": "+15550000002",
            "webhook_url": "https://tc.example/webhooks/telnyx/voice",
            "connection_id": "app-1",
            "stream_url": (
                f"wss://tc.example/ws/telnyx/media-stream/{call_id}/"
                f"{media_token(call_id)}"
            ),
        }
        dial.carrier.assert_not_awaited()

    def test_no_telnyx_key_is_503_naming_it(self, dial) -> None:
        from turncall.services.telnyx_accounts import TelnyxAccount

        dial.number.provider = "telnyx"
        with patch(f"{TELNYX_ACCOUNT}", AsyncMock(return_value=TelnyxAccount("", ""))):
            r = dial.post()
        assert r.status_code == 503
        assert r.json()["code"] == "no_telnyx_account"
        assert r.json()["details"] == {"missing_keys": ["telnyx"]}
        dial.create.assert_not_awaited()

    def test_a_telnyx_number_on_no_application_is_409(self, dial) -> None:
        from turncall.adapters.telephony.telnyx_adapter import TelnyxNumberError
        from turncall.services.telnyx_accounts import TelnyxAccount

        dial.number.provider = "telnyx"
        refused = AsyncMock(side_effect=TelnyxNumberError("on no application"))
        telnyx = AsyncMock()
        with (
            patch(
                f"{TELNYX_ACCOUNT}",
                AsyncMock(return_value=TelnyxAccount("KEY", "PUB")),
            ),
            patch(f"{TELNYX_ADAPTER}.connection_for", refused),
            patch(f"{TELNYX_ADAPTER}.initiate_outbound_call", telnyx),
        ):
            r = dial.post()
        assert r.status_code == 409
        assert "on no application" in r.json()["error"]
        dial.create.assert_not_awaited()  # refused before the call row
        telnyx.assert_not_awaited()

    def test_a_sealed_token_without_the_key_is_503(self, dial) -> None:
        from turncall.storage.repositories.credential_repo import CredentialKeyError

        with patch(
            "turncall.services.outbound_dial.account_for_number",
            side_effect=CredentialKeyError("CREDENTIAL_ENCRYPTION_KEY is not set"),
        ):
            r = dial.post()
        assert r.status_code == 503
        assert r.json()["code"] == "credential_key_unavailable"
        dial.create.assert_not_awaited()
        dial.carrier.assert_not_awaited()

    def test_a_suppressed_number_is_refused_and_never_dialled(self, dial) -> None:
        dial.suppressed = True
        r = dial.post()
        assert r.status_code == 409
        assert r.json()["code"] == "number_suppressed"
        assert "+15550000002" in r.json()["error"]
        dial.create.assert_not_awaited()
        dial.carrier.assert_not_awaited()

    def test_suppression_is_checked_in_the_api_keys_project(self, dial) -> None:
        dial.post()
        assert dial.suppression.await_args.kwargs == {
            "project_id": dial.project_id,
            "number": "+15550000002",
        }

    def test_no_public_base_url_is_409(self, dial) -> None:
        dial.settings = _settings(base_url="")
        r = dial.post()
        assert r.status_code == 409
        dial.carrier.assert_not_awaited()


def _modules_calling(match: Callable[[ast.Attribute], bool]) -> list[str]:
    """Every module under src/ with a call `x.<attr>(...)` that `match` accepts."""
    found = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and match(node.func)
            ):
                found.append(path.relative_to(SRC).as_posix())
                break
    return sorted(found)


# A carrier SDK's own dial: `client.calls.create` on Twilio, `calls.dial` on
# Telnyx. Add the next carrier's here.
_SDK_DIALS = {("calls", "create"), ("calls", "dial")}


def _is_sdk_dial(func: ast.Attribute) -> bool:
    return (
        isinstance(func.value, ast.Attribute)
        and (func.value.attr, func.attr) in _SDK_DIALS
    )


class TestOneDialPath:
    def test_only_the_dial_service_asks_a_carrier_to_dial(self) -> None:
        found = _modules_calling(lambda f: f.attr == "initiate_outbound_call")
        assert found == [DIAL_SERVICE]

    def test_carrier_dial_primitives_stay_inside_the_adapters(self) -> None:
        """A carrier SDK's dial belongs to an adapter's
        `initiate_outbound_call`, which only the service calls. Anywhere else
        it is a dial path the service never sees."""
        found = _modules_calling(_is_sdk_dial)
        assert found, "the guard no longer sees the Twilio dial at all"
        assert all(p.startswith(CARRIER_ADAPTERS) for p in found), found
