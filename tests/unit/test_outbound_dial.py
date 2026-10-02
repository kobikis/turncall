"""Every outbound call goes through one dial service (#213).

The concurrency limit, the calls-per-second limit, the suppression list and
campaigns all hook in there, so a second dial path would bypass every one of
them silently. The route tests pin `POST /v1/calls/outbound` as it behaved
before the move; the guard fails on a new path.
"""

import ast
import uuid
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

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
ADAPTER = "turncall.adapters.telephony.twilio_adapter.TwilioAdapter"
CALLS = "turncall.storage.repositories.call_repo"
AGENTS = "turncall.storage.repositories.agent_repo"
NUMBERS = "turncall.storage.repositories.phone_number_repo"
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
    state = SimpleNamespace(settings=_settings())
    monkeypatch.setattr(
        "turncall.services.outbound_dial.get_settings", lambda: state.settings
    )
    agent = SimpleNamespace(id=uuid.uuid4())
    number = SimpleNamespace(
        id=uuid.uuid4(),
        e164_number="+15550000001",
        twilio_account_sid=None,
        twilio_auth_token_ciphertext=None,
    )
    created = _call(active_agent_id=agent.id)
    state.agent, state.number, state.created = agent, number, created
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

    def test_no_public_base_url_is_409(self, dial) -> None:
        dial.settings = _settings(base_url="")
        r = dial.post()
        assert r.status_code == 409
        dial.carrier.assert_not_awaited()


def _calls_to(attr: str) -> list[str]:
    """Every module under src/ that calls `<something>.<attr>(...)`."""
    found = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr
            ):
                found.append(path.relative_to(SRC).as_posix())
                break
    return sorted(found)


class TestOneDialPath:
    def test_only_the_dial_service_asks_a_carrier_to_dial(self) -> None:
        assert _calls_to("initiate_outbound_call") == [DIAL_SERVICE]

    def test_carrier_dial_primitives_stay_inside_the_adapters(self) -> None:
        """A carrier SDK's own dial (`client.calls.create` on Twilio,
        `calls.dial` on Telnyx) belongs to an adapter's
        `initiate_outbound_call`, which only the service calls. Anywhere else
        it is a dial path the service never sees."""
        primitives = {("calls", "create"), ("calls", "dial")}
        found = []
        for path in SRC.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Attribute)
                    and (node.func.value.attr, node.func.attr) in primitives
                ):
                    found.append(path.relative_to(SRC).as_posix())
        assert found, "the guard no longer sees the Twilio dial at all"
        assert all(p.startswith(CARRIER_ADAPTERS) for p in found), found
