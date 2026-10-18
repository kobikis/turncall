"""Searching and buying a number through the API (#319, #320, ADR-0025).

`GET /v1/phone-numbers/available` and `POST /v1/phone-numbers/purchase` are
admin-only: buying spends the operator's money. Both run on the Twilio
account the request names (`twilio_account_sid` + `twilio_auth_token`, in
headers on the search and in the body on the buy) or, without one, the
platform's TWILIO_* account. A buy answers with what binding the number
needs, and is recorded in the audit log once Twilio has sold it.

On Telnyx (#320) both run on the project's Telnyx account: its `telnyx`
credential, else the platform's TELNYX_API_KEY. A Telnyx purchase may answer
"pending": its number order had not finished.

The end-to-end half is `tests/integration/test_harness_number_purchase.py`;
the adapter halves are `test_twilio_number_purchase.py` and
`test_telnyx_number_purchase.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from twilio.base.exceptions import TwilioRestException

from turncall.adapters.telephony.telnyx_adapter import (
    TelnyxApiError,
    TelnyxOrderFailed,
)
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

AVAILABLE = {
    "e164_number": E164,
    "friendly_name": "(312) 555-0100",
    "country": "US",
    "region": "IL",
    "locality": "Chicago",
    "postal_code": "60601",
    "number_type": "local",
    "capabilities": {"voice": True, "sms": True, "mms": False},
}
BOUGHT = {
    "number_sid": PN,
    "e164_number": E164,
    "friendly_name": "(312) 555-0100",
    "status": "active",
}


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
def as_role(app, session, monkeypatch):
    """Call the app with a key of this role; the platform Twilio account set."""

    def use(role: ProjectRole, *, platform: bool = True) -> Any:
        ctx = AuthContext(project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=role)
        app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
        app.app.dependency_overrides[get_session] = lambda: session
        monkeypatch.setattr(f"{ROUTES}.get_settings", lambda: _settings(platform))
        return app

    return use


@pytest.fixture
def admin(as_role) -> Any:
    return as_role(ProjectRole.ADMIN)


class TestRoles:
    @pytest.mark.parametrize("role", [ProjectRole.DEVELOPER, ProjectRole.VIEWER])
    def test_only_an_admin_key_searches_or_buys(self, as_role, session, role) -> None:
        app = as_role(role)
        with (
            patch(f"{ADAPTER}.search_numbers", AsyncMock()) as search,
            patch(f"{ADAPTER}.buy_number", AsyncMock()) as buy,
        ):
            searched = app.get("/v1/phone-numbers/available", headers=H)
            bought = app.post(
                "/v1/phone-numbers/purchase", headers=H, json={"e164_number": E164}
            )

        for refused in (searched, bought):
            assert refused.status_code == 403, refused.text
            assert refused.json()["code"] == "forbidden"
        search.assert_not_awaited()
        buy.assert_not_awaited()
        session.execute.assert_not_awaited()


class TestSearch:
    def test_searches_the_platform_account_with_the_filters(self, admin) -> None:
        with patch(
            f"{ADAPTER}.search_numbers", AsyncMock(return_value=[AVAILABLE])
        ) as search:
            r = admin.get(
                "/v1/phone-numbers/available",
                headers=H,
                params={"country": "us", "area_code": "312", "contains": "555"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["data"] == [
            {
                "carrier": "twilio",
                **AVAILABLE,
                # Twilio's search does not say what a number costs.
                "upfront_cost": None,
                "monthly_cost": None,
                "currency": None,
            }
        ]
        search.assert_awaited_once_with(
            "US", number_type="local", area_code="312", contains="555", limit=20
        )

    def test_searches_the_account_the_headers_name(self, admin) -> None:
        accounts: list[str] = []

        def adapter(account: Any) -> Any:
            accounts.append(account.account_sid)
            return SimpleNamespace(search_numbers=AsyncMock(return_value=[]))

        with patch("turncall.services.twilio_accounts.TwilioAccount.adapter", adapter):
            r = admin.get(
                "/v1/phone-numbers/available",
                headers={
                    **H,
                    "X-Twilio-Account-Sid": OWN_SID,
                    "X-Twilio-Auth-Token": OWN_TOKEN,
                },
            )

        assert r.status_code == 200, r.text
        assert accounts == [OWN_SID]

    def test_half_an_account_is_refused(self, admin) -> None:
        r = admin.get(
            "/v1/phone-numbers/available",
            headers={**H, "X-Twilio-Account-Sid": OWN_SID},
        )
        assert r.status_code == 422
        assert "together" in r.json()["error"]

    @pytest.mark.parametrize("carrier", ["asterisk", "sip"])
    def test_a_carrier_without_a_number_store_is_refused(self, admin, carrier) -> None:
        with patch(f"{ADAPTER}.search_numbers", AsyncMock()) as search:
            r = admin.get(
                "/v1/phone-numbers/available", headers=H, params={"carrier": carrier}
            )
        assert r.status_code == 422
        search.assert_not_awaited()

    @pytest.mark.parametrize(
        "params",
        [
            {"country": "USA"},
            {"contains": "5"},
            {"contains": "55a"},
            {"area_code": "3x2"},
            {"number_type": "premium"},
            {"limit": 0},
            {"limit": 51},
        ],
    )
    def test_filters_are_checked(self, admin, params) -> None:
        r = admin.get("/v1/phone-numbers/available", headers=H, params=params)
        assert r.status_code == 422, params

    def test_no_account_at_all_is_a_503(self, as_role) -> None:
        app = as_role(ProjectRole.ADMIN, platform=False)
        r = app.get("/v1/phone-numbers/available", headers=H)
        assert r.status_code == 503
        assert r.json()["code"] == "no_twilio_account"

    def test_a_type_twilio_does_not_sell_there_is_a_422(self, admin) -> None:
        missing = TwilioRestException(404, "uri", msg="Not Found", code=20404)
        with patch(f"{ADAPTER}.search_numbers", AsyncMock(side_effect=missing)):
            r = admin.get(
                "/v1/phone-numbers/available",
                headers=H,
                params={"country": "US", "number_type": "mobile"},
            )
        assert r.status_code == 422
        assert "mobile" in r.json()["error"]

    def test_rejected_own_credentials_are_a_422(self, admin) -> None:
        refused = TwilioRestException(401, "uri", msg="Authenticate")
        with patch(f"{ADAPTER}.search_numbers", AsyncMock(side_effect=refused)):
            r = admin.get(
                "/v1/phone-numbers/available",
                headers={
                    **H,
                    "X-Twilio-Account-Sid": OWN_SID,
                    "X-Twilio-Auth-Token": OWN_TOKEN,
                },
            )
        assert r.status_code == 422

    def test_rejected_platform_credentials_are_twilios_error(self, admin) -> None:
        refused = TwilioRestException(401, "uri", msg="Authenticate")
        with patch(f"{ADAPTER}.search_numbers", AsyncMock(side_effect=refused)):
            r = admin.get("/v1/phone-numbers/available", headers=H)
        assert r.status_code == 502
        assert r.json()["code"] == "twilio_error"


class TestBuy:
    def test_buys_on_the_platform_account_and_returns_what_bind_needs(
        self, admin, session
    ) -> None:
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{ADAPTER}.buy_number", AsyncMock(return_value=BOUGHT)) as buy,
        ):
            r = admin.post(
                "/v1/phone-numbers/purchase", headers=H, json={"e164_number": E164}
            )

        assert r.status_code == 201, r.text
        buy.assert_awaited_once_with(E164)
        assert r.json()["data"] == {
            "carrier": "twilio",
            "e164_number": E164,
            "external_number_sid": PN,
            "friendly_name": "(312) 555-0100",
            "status": "active",
            "twilio_account_sid": None,
            "twilio_credentials": "platform",
            "telnyx_order_id": None,
        }
        assert r.headers[AUDIT_HEADER]

    def test_buys_on_the_account_the_body_names(self, admin) -> None:
        accounts: list[str] = []

        def adapter(account: Any) -> Any:
            accounts.append(account.account_sid)
            return SimpleNamespace(buy_number=AsyncMock(return_value=BOUGHT))

        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch("turncall.services.twilio_accounts.TwilioAccount.adapter", adapter),
        ):
            r = admin.post(
                "/v1/phone-numbers/purchase",
                headers=H,
                json={
                    "carrier": "twilio",
                    "e164_number": E164,
                    "twilio_account_sid": OWN_SID,
                    "twilio_auth_token": OWN_TOKEN,
                },
            )

        assert r.status_code == 201, r.text
        assert accounts == [OWN_SID]
        data = r.json()["data"]
        # Bind it with the same pair: the token is never echoed.
        assert data["twilio_account_sid"] == OWN_SID
        assert data["twilio_credentials"] == "number"
        assert OWN_TOKEN not in r.text

    def test_the_purchase_is_audited(self, admin, monkeypatch) -> None:
        from turncall.services.audit_log import AuditTrail

        recorded: list[tuple[Any, ...]] = []
        real = AuditTrail.record

        def record(self, resource_type, resource_id, action, **kw):
            recorded.append((resource_type, resource_id, action, kw["after"].public))
            return real(self, resource_type, resource_id, action, **kw)

        monkeypatch.setattr(AuditTrail, "record", record)
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{ADAPTER}.buy_number", AsyncMock(return_value=BOUGHT)),
        ):
            r = admin.post(
                "/v1/phone-numbers/purchase", headers=H, json={"e164_number": E164}
            )

        assert r.status_code == 201, r.text
        assert recorded == [
            (
                "phone_number",
                E164,
                "purchased",
                {
                    "carrier": "twilio",
                    "e164_number": E164,
                    "external_number_sid": PN,
                    "twilio_account_sid": None,
                },
            )
        ]

    def test_a_bound_number_is_not_bought(self, admin) -> None:
        bound = SimpleNamespace(project_id=uuid.uuid4())
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=bound)),
            patch(f"{ADAPTER}.buy_number", AsyncMock()) as buy,
        ):
            r = admin.post(
                "/v1/phone-numbers/purchase", headers=H, json={"e164_number": E164}
            )
        assert r.status_code == 409
        buy.assert_not_awaited()

    def test_a_number_twilio_will_not_sell_is_a_422_and_unrecorded(self, admin) -> None:
        gone = TwilioRestException(
            400, "uri", msg="Phone number is not available", code=21422
        )
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{ADAPTER}.buy_number", AsyncMock(side_effect=gone)),
        ):
            r = admin.post(
                "/v1/phone-numbers/purchase", headers=H, json={"e164_number": E164}
            )
        assert r.status_code == 422
        body = r.json()
        assert "not available" in body["error"]
        assert body["details"] == {"twilio_code": 21422}
        assert AUDIT_HEADER not in r.headers

    def test_twilio_failing_is_a_502(self, admin) -> None:
        down = TwilioRestException(500, "uri", msg="Internal")
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{ADAPTER}.buy_number", AsyncMock(side_effect=down)),
        ):
            r = admin.post(
                "/v1/phone-numbers/purchase", headers=H, json={"e164_number": E164}
            )
        assert r.status_code == 502
        assert r.json()["code"] == "twilio_error"

    @pytest.mark.parametrize(
        "body",
        [
            {"e164_number": "3125550100"},
            {"e164_number": E164, "carrier": "asterisk"},
            {"e164_number": E164, "twilio_account_sid": OWN_SID},
            {
                "e164_number": E164,
                "carrier": "telnyx",
                "twilio_account_sid": OWN_SID,
                "twilio_auth_token": OWN_TOKEN,
            },
        ],
    )
    def test_the_body_is_checked(self, admin, body) -> None:
        with patch(f"{ADAPTER}.buy_number", AsyncMock()) as buy:
            r = admin.post("/v1/phone-numbers/purchase", headers=H, json=body)
        assert r.status_code == 422, body
        buy.assert_not_awaited()

    def test_no_account_at_all_is_a_503(self, as_role) -> None:
        app = as_role(ProjectRole.ADMIN, platform=False)
        with patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)):
            r = app.post(
                "/v1/phone-numbers/purchase", headers=H, json={"e164_number": E164}
            )
        assert r.status_code == 503
        assert r.json()["code"] == "no_twilio_account"


# --- Telnyx (#320) ---------------------------------------------------------

TELNYX_AVAILABLE = {
    **AVAILABLE,
    "friendly_name": None,
    "postal_code": None,
    "upfront_cost": "1.00",
    "monthly_cost": "1.00",
    "currency": "USD",
}


def _telnyx_bought(status: str = "active") -> dict[str, Any]:
    return {
        "number_sid": "pn-777" if status == "active" else None,
        "e164_number": E164,
        "friendly_name": None,
        "status": status,
        "order_id": "order-1",
    }


@pytest.fixture
def telnyx_account(monkeypatch) -> list[Any]:
    """The project's Telnyx account (its `telnyx` credential); the projects
    it was resolved for."""
    from turncall.services.telnyx_accounts import TelnyxAccount

    asked: list[Any] = []

    async def account_for_project(session, project_id, settings):
        asked.append(project_id)
        return TelnyxAccount("PROJECT_KEY", "pub")

    monkeypatch.setattr(ACCOUNTS, account_for_project)
    return asked


class TestTelnyxRoles:
    @pytest.mark.parametrize("role", [ProjectRole.DEVELOPER, ProjectRole.VIEWER])
    def test_only_an_admin_key_searches_or_buys(
        self, as_role, session, telnyx_account, role
    ) -> None:
        app = as_role(role)
        with (
            patch(f"{TELNYX}.search_numbers", AsyncMock()) as search,
            patch(f"{TELNYX}.buy_number", AsyncMock()) as buy,
        ):
            searched = app.get(
                "/v1/phone-numbers/available", headers=H, params={"carrier": "telnyx"}
            )
            bought = app.post(
                "/v1/phone-numbers/purchase",
                headers=H,
                json={"carrier": "telnyx", "e164_number": E164},
            )

        for refused in (searched, bought):
            assert refused.status_code == 403, refused.text
        search.assert_not_awaited()
        buy.assert_not_awaited()
        assert telnyx_account == []


class TestTelnyxSearch:
    def test_searches_the_projects_telnyx_account(self, admin, telnyx_account) -> None:
        keys: list[str] = []

        def adapter(account: Any) -> Any:
            keys.append(account.api_key)
            return SimpleNamespace(
                search_numbers=AsyncMock(return_value=[TELNYX_AVAILABLE])
            )

        with patch("turncall.services.telnyx_accounts.TelnyxAccount.adapter", adapter):
            r = admin.get(
                "/v1/phone-numbers/available",
                headers=H,
                params={"carrier": "telnyx", "country": "us", "area_code": "312"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["data"] == [{"carrier": "telnyx", **TELNYX_AVAILABLE}]
        assert keys == ["PROJECT_KEY"]
        assert len(telnyx_account) == 1

    def test_passes_the_filters(self, admin, telnyx_account) -> None:
        with patch(f"{TELNYX}.search_numbers", AsyncMock(return_value=[])) as search:
            r = admin.get(
                "/v1/phone-numbers/available",
                headers=H,
                params={
                    "carrier": "telnyx",
                    "country": "gb",
                    "number_type": "mobile",
                    "contains": "77",
                    "limit": 3,
                },
            )
        assert r.status_code == 200, r.text
        search.assert_awaited_once_with(
            "GB", number_type="mobile", area_code=None, contains="77", limit=3
        )

    def test_twilio_headers_are_refused(self, admin, telnyx_account) -> None:
        r = admin.get(
            "/v1/phone-numbers/available",
            headers={
                **H,
                "X-Twilio-Account-Sid": OWN_SID,
                "X-Twilio-Auth-Token": OWN_TOKEN,
            },
            params={"carrier": "telnyx"},
        )
        assert r.status_code == 422

    def test_no_telnyx_account_is_a_503(self, admin, monkeypatch) -> None:
        from turncall.services.telnyx_accounts import TelnyxAccount

        async def none(session, project_id, settings):
            return TelnyxAccount("", "")

        monkeypatch.setattr(ACCOUNTS, none)
        r = admin.get(
            "/v1/phone-numbers/available", headers=H, params={"carrier": "telnyx"}
        )
        assert r.status_code == 503
        body = r.json()
        assert body["code"] == "no_telnyx_account"
        assert body["details"] == {"missing_keys": ["telnyx"]}
        assert "search for numbers" in body["error"]

    @pytest.mark.parametrize(
        ("status", "code"),
        [(401, "validation_error"), (400, "validation_error"), (500, "telnyx_error")],
    )
    def test_telnyx_refusing_is_mapped(
        self, admin, telnyx_account, status, code
    ) -> None:
        refused = TelnyxApiError(status, ["1"], "no")
        with patch(f"{TELNYX}.search_numbers", AsyncMock(side_effect=refused)):
            r = admin.get(
                "/v1/phone-numbers/available", headers=H, params={"carrier": "telnyx"}
            )
        assert r.status_code == (502 if status == 500 else 422)
        assert r.json()["code"] == code


class TestTelnyxBuy:
    def _buy(self, admin) -> Any:
        return admin.post(
            "/v1/phone-numbers/purchase",
            headers=H,
            json={"carrier": "telnyx", "e164_number": E164},
        )

    def test_buys_and_returns_what_bind_needs(self, admin, telnyx_account) -> None:
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(
                f"{TELNYX}.buy_number", AsyncMock(return_value=_telnyx_bought())
            ) as buy,
        ):
            r = self._buy(admin)

        assert r.status_code == 201, r.text
        buy.assert_awaited_once_with(E164)
        assert r.json()["data"] == {
            "carrier": "telnyx",
            "e164_number": E164,
            "external_number_sid": "pn-777",
            "friendly_name": None,
            "status": "active",
            "twilio_account_sid": None,
            "twilio_credentials": None,
            "telnyx_order_id": "order-1",
        }
        assert r.headers[AUDIT_HEADER]

    def test_an_unfinished_order_is_pending_and_still_audited(
        self, admin, telnyx_account, monkeypatch
    ) -> None:
        from turncall.services.audit_log import AuditTrail

        recorded: list[tuple[Any, ...]] = []
        real = AuditTrail.record

        def record(self, resource_type, resource_id, action, **kw):
            recorded.append((resource_type, resource_id, action, kw["after"].public))
            return real(self, resource_type, resource_id, action, **kw)

        monkeypatch.setattr(AuditTrail, "record", record)
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(
                f"{TELNYX}.buy_number",
                AsyncMock(return_value=_telnyx_bought("pending")),
            ),
        ):
            r = self._buy(admin)

        assert r.status_code == 201, r.text
        data = r.json()["data"]
        assert data["status"] == "pending"
        assert data["external_number_sid"] is None
        assert data["telnyx_order_id"] == "order-1"
        # Ordered is bought: the money is spent, so it is in the audit log.
        assert recorded == [
            (
                "phone_number",
                E164,
                "purchased",
                {
                    "carrier": "telnyx",
                    "e164_number": E164,
                    "external_number_sid": None,
                    "status": "pending",
                    "telnyx_order_id": "order-1",
                },
            )
        ]

    def test_a_failed_order_is_a_422_and_unrecorded(
        self, admin, telnyx_account
    ) -> None:
        failed = TelnyxOrderFailed("order-1", E164)
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{TELNYX}.buy_number", AsyncMock(side_effect=failed)),
        ):
            r = self._buy(admin)
        assert r.status_code == 422
        assert r.json()["details"] == {"telnyx_order_id": "order-1"}
        assert AUDIT_HEADER not in r.headers

    def test_a_number_telnyx_will_not_sell_is_a_422(
        self, admin, telnyx_account
    ) -> None:
        gone = TelnyxApiError(422, ["85001"], "Number not available")
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{TELNYX}.buy_number", AsyncMock(side_effect=gone)),
        ):
            r = self._buy(admin)
        assert r.status_code == 422
        body = r.json()
        assert "not available" in body["error"]
        assert body["details"] == {"telnyx_codes": ["85001"]}
        assert AUDIT_HEADER not in r.headers

    def test_telnyx_failing_is_a_502(self, admin, telnyx_account) -> None:
        down = TelnyxApiError(503, [], "unavailable")
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)),
            patch(f"{TELNYX}.buy_number", AsyncMock(side_effect=down)),
        ):
            r = self._buy(admin)
        assert r.status_code == 502
        assert r.json()["code"] == "telnyx_error"

    def test_a_bound_number_is_not_bought(self, admin, telnyx_account) -> None:
        bound = SimpleNamespace(project_id=uuid.uuid4())
        with (
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=bound)),
            patch(f"{TELNYX}.buy_number", AsyncMock()) as buy,
        ):
            r = self._buy(admin)
        assert r.status_code == 409
        buy.assert_not_awaited()
