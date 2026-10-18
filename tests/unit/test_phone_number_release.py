"""Releasing a number through the API (#321, ADR-0025).

`POST /v1/phone-numbers/release` gives a number on the carrier account back
to the carrier. It is admin-only and irreversible, so it is refused while
TurnCall binds the number (409: a number is bound exactly when it has a
`phone_numbers` row) and without `confirm` repeating the number (422). On
Twilio it runs on the account the body names, else the platform's; on Telnyx
on the project's Telnyx account, as a purchase does. It is audited as
`phone_number` / `released`, keyed by the E.164 number, once the carrier has
released it. Unbinding (`DELETE /v1/phone-numbers/{id}`) never releases.

On the platform's shared account, only a number the project bought is
released: here `bought` stands in for that record. The refusals are
`test_phone_number_release_shared_account.py`.

The end-to-end half is `tests/integration/test_harness_number_release.py`;
the adapter halves are in `test_twilio_number_purchase.py` and
`test_telnyx_number_purchase.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from twilio.base.exceptions import TwilioRestException

from turncall.adapters.telephony.telnyx_adapter import TelnyxApiError
from turncall.api.audit import AUDIT_HEADER
from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
OWN_SID = "AC" + "a" * 32
OWN_TOKEN = "own-twilio-auth-token"
PLATFORM_SID = "AC" + "p" * 32
ADAPTER = "turncall.adapters.telephony.twilio_adapter.TwilioAdapter"
TELNYX = "turncall.adapters.telephony.telnyx_adapter.TelnyxAdapter"
ACCOUNTS = "turncall.services.telnyx_accounts.account_for_project"
REPO = "turncall.storage.repositories.phone_number_repo"
ROUTES = "turncall.api.v1.phone_number_purchase"
E164 = "+13125550100"
PN = "PN" + "b" * 32
URL = "/v1/phone-numbers/release"


def _release(carrier: str = "twilio", **extra: Any) -> dict[str, Any]:
    return {"carrier": carrier, "e164_number": E164, "confirm": E164, **extra}


def _settings(platform: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        twilio=SimpleNamespace(
            account_sid=PLATFORM_SID if platform else "",
            auth_token="platform-token" if platform else "",
        )
    )


@pytest.fixture
def session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def project_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def as_role(app, session, project_id, monkeypatch):
    """Call the app with a key of this role; the platform Twilio account set."""

    def use(role: ProjectRole, *, platform: bool = True) -> Any:
        ctx = AuthContext(project_id=project_id, api_key_id=uuid.uuid4(), role=role)
        app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
        app.app.dependency_overrides[get_session] = lambda: session
        monkeypatch.setattr(f"{ROUTES}.get_settings", lambda: _settings(platform))
        return app

    return use


@pytest.fixture
def admin(as_role) -> Any:
    return as_role(ProjectRole.ADMIN)


@pytest.fixture
def unbound() -> Any:
    with patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)) as lookup:
        yield lookup


@pytest.fixture
def bought() -> Any:
    """The project bought the number on the platform's account."""
    with patch(
        "turncall.storage.repositories.purchased_number_repo.bought_on_platform",
        AsyncMock(return_value=True),
    ) as lookup:
        yield lookup


@pytest.fixture
def telnyx_account(monkeypatch) -> list[Any]:
    """The project's Telnyx account (its `telnyx` credential); the projects
    it was resolved for."""
    from turncall.services.telnyx_accounts import TelnyxAccount

    asked: list[Any] = []

    async def account_for_project(session, project_id, settings):
        asked.append(project_id)
        return TelnyxAccount("PROJECT_KEY", "pub", shared=False)

    monkeypatch.setattr(ACCOUNTS, account_for_project)
    return asked


@pytest.fixture
def audited(monkeypatch) -> list[tuple[Any, ...]]:
    """Each audit entry recorded: (type, id, action, before, after)."""
    from turncall.services.audit_log import AuditTrail

    recorded: list[tuple[Any, ...]] = []
    real = AuditTrail.record

    def record(self, resource_type, resource_id, action, **kw):
        before, after = kw.get("before"), kw.get("after")
        recorded.append(
            (
                resource_type,
                resource_id,
                action,
                before.public if before else None,
                after.public if after else None,
            )
        )
        return real(self, resource_type, resource_id, action, **kw)

    monkeypatch.setattr(AuditTrail, "record", record)
    return recorded


class TestRoles:
    @pytest.mark.parametrize("role", [ProjectRole.DEVELOPER, ProjectRole.VIEWER])
    @pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
    def test_only_an_admin_key_releases(
        self, as_role, session, telnyx_account, role, carrier
    ) -> None:
        app = as_role(role)
        with (
            patch(f"{ADAPTER}.release_number", AsyncMock()) as twilio,
            patch(f"{TELNYX}.release_number", AsyncMock()) as telnyx,
        ):
            r = app.post(URL, headers=H, json=_release(carrier))

        assert r.status_code == 403, r.text
        assert r.json()["code"] == "forbidden"
        twilio.assert_not_awaited()
        telnyx.assert_not_awaited()
        session.execute.assert_not_awaited()
        assert telnyx_account == []


class TestConfirmation:
    @pytest.mark.parametrize(
        "confirm",
        [
            {},  # absent
            {"confirm": None},
            {"confirm": True},
            {"confirm": "true"},
            {"confirm": "yes"},
            {"confirm": "+13125550199"},  # another number
            {"confirm": "13125550100"},  # not exactly the number
        ],
    )
    @pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
    def test_without_the_number_repeated_nothing_is_released(
        self, admin, telnyx_account, confirm, carrier
    ) -> None:
        body = {"carrier": carrier, "e164_number": E164, **confirm}
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock()) as lookup,
            patch(f"{ADAPTER}.release_number", AsyncMock()) as twilio,
            patch(f"{TELNYX}.release_number", AsyncMock()) as telnyx,
        ):
            r = admin.post(URL, headers=H, json=body)

        assert r.status_code == 422, (confirm, r.text)
        assert r.json()["code"] == "validation_error"
        lookup.assert_not_awaited()
        twilio.assert_not_awaited()
        telnyx.assert_not_awaited()
        assert AUDIT_HEADER not in r.headers

    def test_the_refusal_says_what_to_send(self, admin) -> None:
        r = admin.post(URL, headers=H, json={"e164_number": E164})
        assert r.status_code == 422
        [error] = r.json()["details"]["errors"]
        assert "irreversible" in error["msg"]
        assert f"set confirm to the number being released ({E164})" in error["msg"]


class TestBound:
    @pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
    def test_a_bound_number_is_refused_and_named(
        self, admin, project_id, telnyx_account, carrier
    ) -> None:
        row = SimpleNamespace(id=uuid.uuid4(), project_id=project_id)
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=row)),
            patch(f"{ADAPTER}.find_number_sid", AsyncMock()) as find,
            patch(f"{ADAPTER}.release_number", AsyncMock()) as twilio,
            patch(f"{TELNYX}.find_number", AsyncMock()) as find_telnyx,
            patch(f"{TELNYX}.release_number", AsyncMock()) as telnyx,
        ):
            r = admin.post(URL, headers=H, json=_release(carrier))

        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == "conflict"
        assert "unbind" in body["error"]
        # The binding to delete first, when it is this project's.
        assert body["details"] == {"phone_number_id": str(row.id)}
        for untouched in (find, twilio, find_telnyx, telnyx):
            untouched.assert_not_awaited()
        assert telnyx_account == []
        assert AUDIT_HEADER not in r.headers

    def test_another_projects_binding_is_not_named(self, admin) -> None:
        row = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=row)),
            patch(f"{ADAPTER}.release_number", AsyncMock()) as release,
        ):
            r = admin.post(URL, headers=H, json=_release())
        assert r.status_code == 409
        assert r.json()["details"] is None
        assert str(row.id) not in r.text
        release.assert_not_awaited()


class TestTwilio:
    def test_releases_from_the_platform_account_and_audits_it(
        self, admin, unbound, bought, audited
    ) -> None:
        with (
            patch(f"{ADAPTER}.find_number_sid", AsyncMock(return_value=PN)) as find,
            patch(f"{ADAPTER}.release_number", AsyncMock()) as release,
        ):
            r = admin.post(URL, headers=H, json=_release())

        assert r.status_code == 200, r.text
        find.assert_awaited_once_with(E164)
        release.assert_awaited_once_with(PN)
        assert r.json()["data"] == {
            "carrier": "twilio",
            "e164_number": E164,
            "external_number_sid": PN,
            "status": "released",
            "twilio_account_sid": None,
            "twilio_credentials": "platform",
        }
        assert r.headers[AUDIT_HEADER]
        assert audited == [
            (
                "phone_number",
                E164,
                "released",
                {
                    "carrier": "twilio",
                    "e164_number": E164,
                    "external_number_sid": PN,
                    "twilio_account_sid": None,
                },
                None,
            )
        ]

    def test_releases_from_the_account_the_body_names(self, admin, unbound) -> None:
        accounts: list[str] = []
        released: list[str] = []

        def adapter(account: Any) -> Any:
            accounts.append(account.account_sid)
            return SimpleNamespace(
                find_number_sid=AsyncMock(return_value=PN),
                release_number=AsyncMock(side_effect=released.append),
            )

        with patch("turncall.services.twilio_accounts.TwilioAccount.adapter", adapter):
            r = admin.post(
                URL,
                headers=H,
                json=_release(twilio_account_sid=OWN_SID, twilio_auth_token=OWN_TOKEN),
            )

        assert r.status_code == 200, r.text
        assert accounts == [OWN_SID]
        assert released == [PN]
        data = r.json()["data"]
        assert data["twilio_account_sid"] == OWN_SID
        assert data["twilio_credentials"] == "number"
        assert OWN_TOKEN not in r.text

    def test_a_number_not_on_the_account_is_a_422_and_unrecorded(
        self, admin, unbound, bought
    ) -> None:
        with (
            patch(f"{ADAPTER}.find_number_sid", AsyncMock(return_value=None)),
            patch(f"{ADAPTER}.release_number", AsyncMock()) as release,
        ):
            r = admin.post(URL, headers=H, json=_release())
        assert r.status_code == 422
        assert "not a phone number on this Twilio account" in r.json()["error"]
        release.assert_not_awaited()
        assert AUDIT_HEADER not in r.headers

    def test_a_number_gone_before_the_release_is_a_422(
        self, admin, unbound, bought
    ) -> None:
        gone = TwilioRestException(404, "uri", msg="Not Found", code=20404)
        with (
            patch(f"{ADAPTER}.find_number_sid", AsyncMock(return_value=PN)),
            patch(f"{ADAPTER}.release_number", AsyncMock(side_effect=gone)),
        ):
            r = admin.post(URL, headers=H, json=_release())
        assert r.status_code == 422
        assert AUDIT_HEADER not in r.headers

    def test_rejected_own_credentials_are_a_422(self, admin, unbound) -> None:
        refused = TwilioRestException(401, "uri", msg="Authenticate")
        with patch(f"{ADAPTER}.find_number_sid", AsyncMock(side_effect=refused)):
            r = admin.post(
                URL,
                headers=H,
                json=_release(twilio_account_sid=OWN_SID, twilio_auth_token=OWN_TOKEN),
            )
        assert r.status_code == 422

    def test_twilio_failing_is_a_502(self, admin, unbound, bought) -> None:
        down = TwilioRestException(500, "uri", msg="Internal")
        with (
            patch(f"{ADAPTER}.find_number_sid", AsyncMock(return_value=PN)),
            patch(f"{ADAPTER}.release_number", AsyncMock(side_effect=down)),
        ):
            r = admin.post(URL, headers=H, json=_release())
        assert r.status_code == 502
        assert r.json()["code"] == "twilio_error"
        assert AUDIT_HEADER not in r.headers

    def test_no_account_at_all_is_a_503(self, as_role, unbound) -> None:
        app = as_role(ProjectRole.ADMIN, platform=False)
        r = app.post(URL, headers=H, json=_release())
        assert r.status_code == 503
        assert r.json()["code"] == "no_twilio_account"

    @pytest.mark.parametrize(
        "body",
        [
            {"e164_number": "3125550100", "confirm": "3125550100"},
            _release("asterisk"),
            _release(twilio_account_sid=OWN_SID),
            _release("telnyx", twilio_account_sid=OWN_SID, twilio_auth_token=OWN_TOKEN),
        ],
    )
    def test_the_body_is_checked(self, admin, body) -> None:
        with patch(f"{ADAPTER}.release_number", AsyncMock()) as release:
            r = admin.post(URL, headers=H, json=body)
        assert r.status_code == 422, body
        release.assert_not_awaited()


class TestTelnyx:
    def test_releases_from_the_projects_account_and_audits_it(
        self, admin, unbound, telnyx_account, audited
    ) -> None:
        keys: list[str] = []
        released: list[str] = []

        def adapter(account: Any) -> Any:
            keys.append(account.api_key)
            return SimpleNamespace(
                find_number=AsyncMock(
                    return_value={"id": "pn-777", "phone_number": E164}
                ),
                release_number=AsyncMock(side_effect=released.append),
            )

        with patch("turncall.services.telnyx_accounts.TelnyxAccount.adapter", adapter):
            r = admin.post(URL, headers=H, json=_release("telnyx"))

        assert r.status_code == 200, r.text
        assert keys == ["PROJECT_KEY"]
        # The phone number's id, as Telnyx's DELETE /phone_numbers/{id} takes it.
        assert released == ["pn-777"]
        assert r.json()["data"] == {
            "carrier": "telnyx",
            "e164_number": E164,
            "external_number_sid": "pn-777",
            "status": "released",
            "twilio_account_sid": None,
            "twilio_credentials": None,
        }
        assert audited == [
            (
                "phone_number",
                E164,
                "released",
                {
                    "carrier": "telnyx",
                    "e164_number": E164,
                    "external_number_sid": "pn-777",
                },
                None,
            )
        ]

    def test_a_number_not_on_the_account_is_a_422(
        self, admin, unbound, telnyx_account
    ) -> None:
        with (
            patch(f"{TELNYX}.find_number", AsyncMock(return_value=None)),
            patch(f"{TELNYX}.release_number", AsyncMock()) as release,
        ):
            r = admin.post(URL, headers=H, json=_release("telnyx"))
        assert r.status_code == 422
        assert "Telnyx account" in r.json()["error"]
        release.assert_not_awaited()
        assert AUDIT_HEADER not in r.headers

    @pytest.mark.parametrize(
        ("status", "expected", "code"),
        [
            (401, 422, "validation_error"),
            (404, 422, "validation_error"),
            (422, 422, "validation_error"),
            (500, 502, "telnyx_error"),
        ],
    )
    def test_telnyx_refusing_is_mapped(
        self, admin, unbound, telnyx_account, status, expected, code
    ) -> None:
        refused = TelnyxApiError(status, ["1"], "no")
        with (
            patch(f"{TELNYX}.find_number", AsyncMock(return_value={"id": "pn-777"})),
            patch(f"{TELNYX}.release_number", AsyncMock(side_effect=refused)),
        ):
            r = admin.post(URL, headers=H, json=_release("telnyx"))
        assert r.status_code == expected
        assert r.json()["code"] == code
        assert AUDIT_HEADER not in r.headers

    def test_telnyx_not_answering_the_lookup_is_a_502(
        self, admin, unbound, telnyx_account
    ) -> None:
        with (
            patch(
                f"{TELNYX}.find_number",
                AsyncMock(side_effect=httpx.ConnectTimeout("slow")),
            ),
            patch(f"{TELNYX}.release_number", AsyncMock()) as release,
        ):
            r = admin.post(URL, headers=H, json=_release("telnyx"))
        assert r.status_code == 502, r.text
        assert r.json()["code"] == "telnyx_error"
        release.assert_not_awaited()
        assert AUDIT_HEADER not in r.headers

    def test_telnyx_not_answering_the_release_says_it_may_be_released(
        self, admin, unbound, telnyx_account
    ) -> None:
        from loguru import logger

        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(str(m)), level="ERROR")
        try:
            with (
                patch(
                    f"{TELNYX}.find_number", AsyncMock(return_value={"id": "pn-777"})
                ),
                patch(
                    f"{TELNYX}.release_number",
                    AsyncMock(side_effect=httpx.ReadTimeout("slow")),
                ),
            ):
                r = admin.post(URL, headers=H, json=_release("telnyx"))
        finally:
            logger.remove(sink)
        assert r.status_code == 502, r.text
        assert r.json()["code"] == "telnyx_error"
        assert "may have been released" in r.json()["error"]
        assert AUDIT_HEADER not in r.headers
        [logged] = [m for m in seen if "telnyx_number_release_unanswered" in m]
        assert "pn-777" in logged

    def test_no_transaction_is_held_open_while_telnyx_answers(
        self, admin, session, unbound, telnyx_account
    ) -> None:
        seen: list[int] = []

        async def release(number_id: str) -> None:
            seen.append(session.rollback.await_count)

        with (
            patch(f"{TELNYX}.find_number", AsyncMock(return_value={"id": "pn-777"})),
            patch(f"{TELNYX}.release_number", side_effect=release),
        ):
            r = admin.post(URL, headers=H, json=_release("telnyx"))
        assert r.status_code == 200, r.text
        assert seen == [1]

    def test_no_telnyx_account_is_a_503(self, admin, unbound, monkeypatch) -> None:
        from turncall.services.telnyx_accounts import TelnyxAccount

        async def none(session, project_id, settings):
            return TelnyxAccount("", "", shared=False)

        monkeypatch.setattr(ACCOUNTS, none)
        r = admin.post(URL, headers=H, json=_release("telnyx"))
        assert r.status_code == 503
        body = r.json()
        assert body["code"] == "no_telnyx_account"
        assert "release the number" in body["error"]


class TestUnbindIsNotRelease:
    """Unbinding deletes TurnCall's binding and points the number away from
    TurnCall; the number stays on the carrier account, and billed."""

    @staticmethod
    def _row(carrier: str, project_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            project_id=project_id,
            provider=carrier,
            external_number_sid=PN if carrier == "twilio" else "pn-777",
            e164_number=E164,
            routing_target_type="agent",
            routing_target_id=uuid.uuid4(),
            server_url=None,
            server_url_secret=None,
            sms_enabled=False,
            routing_weights=None,
            metadata_json={},
            created_at=datetime.now(UTC),
            twilio_account_sid=None,
            twilio_auth_token_ciphertext=None,
        )

    @pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
    def test_unbinding_never_releases(
        self, as_role, project_id, telnyx_account, monkeypatch, carrier
    ) -> None:
        app = as_role(ProjectRole.DEVELOPER)  # a write key unbinds; no admin needed
        monkeypatch.setattr(
            "turncall.api.v1.phone_numbers.get_settings", lambda: _settings()
        )
        row = self._row(carrier, project_id)
        with (
            patch(f"{REPO}.get_by_id", AsyncMock(return_value=row)),
            patch(f"{REPO}.delete_phone_number", AsyncMock()) as deleted,
            patch(f"{ADAPTER}.configure_number_webhook", AsyncMock()) as twilio_route,
            patch(f"{ADAPTER}.configure_sms_webhook", AsyncMock()),
            patch(f"{ADAPTER}.release_number", AsyncMock()) as twilio_release,
            patch(f"{TELNYX}.configure_number_webhook", AsyncMock()) as telnyx_route,
            patch(f"{TELNYX}.release_number", AsyncMock()) as telnyx_release,
        ):
            r = app.delete(f"/v1/phone-numbers/{row.id}", headers=H)

        assert r.status_code == 200, r.text
        assert r.json()["data"] == {"deleted": True}
        deleted.assert_awaited_once()
        # Pointed away from TurnCall on its carrier, not given back.
        route = twilio_route if carrier == "twilio" else telnyx_route
        route.assert_awaited_once_with(
            row.external_number_sid, voice_url="", status_callback_url=""
        )
        twilio_release.assert_not_awaited()
        telnyx_release.assert_not_awaited()
