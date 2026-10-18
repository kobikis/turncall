"""Releases on the platform's shared carrier account (#321, ADR-0025).

The platform's Twilio account (TWILIO_*, used when a request sends no account
pair) and its Telnyx account (TELNYX_API_KEY, used when the project holds no
`telnyx` credential) hold every project's numbers, and the operator's own. So
a release that would run on one is allowed only for a number this project
bought there through `POST /v1/phone-numbers/purchase`; anything else is a
403, before the carrier is asked anything. Each purchase records the number
and the account it went on (`purchased_number_repo.record`), and a release
forgets it. A release on the project's own account is not restricted.

The repository's queries run against Postgres in
`tests/integration/test_harness_number_release.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from turncall.api.audit import AUDIT_HEADER
from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole
from turncall.services.telnyx_accounts import TelnyxAccount

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
OWN_SID = "AC" + "a" * 32
OWN_TOKEN = "own-twilio-auth-token"
PLATFORM_SID = "AC" + "p" * 32
ADAPTER = "turncall.adapters.telephony.twilio_adapter.TwilioAdapter"
TELNYX = "turncall.adapters.telephony.telnyx_adapter.TelnyxAdapter"
ACCOUNTS = "turncall.services.telnyx_accounts.account_for_project"
REPO = "turncall.storage.repositories.phone_number_repo"
BOUGHT_REPO = "turncall.storage.repositories.purchased_number_repo"
ROUTES = "turncall.api.v1.phone_number_purchase"
E164 = "+13125550100"
PN = "PN" + "b" * 32
RELEASE = "/v1/phone-numbers/release"
PURCHASE = "/v1/phone-numbers/purchase"
OWN_PAIR = {"twilio_account_sid": OWN_SID, "twilio_auth_token": OWN_TOKEN}


def _release(carrier: str = "twilio", **extra: Any) -> dict[str, Any]:
    return {"carrier": carrier, "e164_number": E164, "confirm": E164, **extra}


@pytest.fixture
def project_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def admin(app, project_id, monkeypatch) -> Any:
    ctx = AuthContext(
        project_id=project_id, api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    settings = SimpleNamespace(
        twilio=SimpleNamespace(account_sid=PLATFORM_SID, auth_token="platform-token")
    )
    monkeypatch.setattr(f"{ROUTES}.get_settings", lambda: settings)
    with patch(f"{REPO}.get_by_e164", AsyncMock(return_value=None)):
        yield app


@pytest.fixture(params=["own", "platform"])
def telnyx_account(request, monkeypatch) -> str:
    """The project's Telnyx account: its `telnyx` credential ("own"), or the
    platform's TELNYX_API_KEY ("platform")."""
    account = TelnyxAccount(
        "PLATFORM_KEY" if request.param == "platform" else "PROJECT_KEY",
        "pub",
        platform=request.param == "platform",
    )

    async def account_for_project(session, project_id, settings):
        return account

    monkeypatch.setattr(ACCOUNTS, account_for_project)
    return request.param


@pytest.fixture
def platform_telnyx(monkeypatch) -> None:
    async def account_for_project(session, project_id, settings):
        return TelnyxAccount("PLATFORM_KEY", "pub", platform=True)

    monkeypatch.setattr(ACCOUNTS, account_for_project)


@pytest.fixture
def own_telnyx(monkeypatch) -> None:
    async def account_for_project(session, project_id, settings):
        return TelnyxAccount("PROJECT_KEY", "pub")

    monkeypatch.setattr(ACCOUNTS, account_for_project)


def _ledger(*, bought: bool) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Patches for `purchased_number_repo`: (bought_on_platform, forget, record)."""
    return (
        AsyncMock(return_value=bought),
        AsyncMock(),
        AsyncMock(),
    )


def _patched(lookup: AsyncMock, forget: AsyncMock, record: AsyncMock) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch(f"{BOUGHT_REPO}.bought_on_platform", lookup))
    stack.enter_context(patch(f"{BOUGHT_REPO}.forget", forget))
    stack.enter_context(patch(f"{BOUGHT_REPO}.record", record))
    return stack


def _carrier() -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    """(Twilio find, Twilio release, Telnyx find, Telnyx release)."""
    return (
        AsyncMock(return_value=PN),
        AsyncMock(),
        AsyncMock(return_value={"id": "pn-777", "phone_number": E164}),
        AsyncMock(),
    )


def _carrier_patched(find: Any, release: Any, find_t: Any, release_t: Any) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch(f"{ADAPTER}.find_number_sid", find))
    stack.enter_context(patch(f"{ADAPTER}.release_number", release))
    stack.enter_context(patch(f"{TELNYX}.find_number", find_t))
    stack.enter_context(patch(f"{TELNYX}.release_number", release_t))
    return stack


class TestTheTelnyxAccountSaysWhoseItIs:
    def test_the_platforms_account_is_shared(self) -> None:
        from turncall.services import telnyx_accounts

        settings = SimpleNamespace(
            telnyx=SimpleNamespace(api_key="KEY", public_key="PUB")
        )
        assert telnyx_accounts.platform_account(settings).platform is True

    async def test_a_project_credential_is_the_projects_own(self) -> None:
        from turncall.services import telnyx_accounts

        settings = SimpleNamespace(
            telnyx=SimpleNamespace(api_key="KEY", public_key="PUB")
        )
        with patch(
            "turncall.storage.repositories.credential_repo.load_documents",
            AsyncMock(return_value={"telnyx": {"api_key": "OWN", "public_key": "P"}}),
        ):
            own = await telnyx_accounts.account_for_project(
                AsyncMock(), uuid.uuid4(), settings
            )
        with patch(
            "turncall.storage.repositories.credential_repo.load_documents",
            AsyncMock(return_value={}),
        ):
            shared = await telnyx_accounts.account_for_project(
                AsyncMock(), uuid.uuid4(), settings
            )
        assert (own.api_key, own.platform) == ("OWN", False)
        assert (shared.api_key, shared.platform) == ("KEY", True)


class TestAPurchaseIsRecorded:
    @pytest.mark.parametrize(
        ("body", "account"),
        [({}, "platform"), (OWN_PAIR, "own")],
    )
    def test_on_twilio_with_the_account_it_went_on(
        self, admin, project_id, body, account
    ) -> None:
        lookup, forget, record = _ledger(bought=False)
        bought = {"number_sid": PN, "e164_number": E164, "status": "active"}
        with (
            _patched(lookup, forget, record),
            patch(f"{ADAPTER}.buy_number", AsyncMock(return_value=bought)),
        ):
            r = admin.post(PURCHASE, headers=H, json={"e164_number": E164, **body})

        assert r.status_code == 201, r.text
        record.assert_awaited_once()
        assert record.await_args.kwargs == {
            "project_id": project_id,
            "carrier": "twilio",
            "e164_number": E164,
            "account": account,
        }

    @pytest.mark.parametrize("status", ["active", "pending"])
    def test_on_telnyx_with_the_account_it_went_on(
        self, admin, project_id, telnyx_account, status
    ) -> None:
        lookup, forget, record = _ledger(bought=False)
        bought = {
            "number_sid": "pn-777" if status == "active" else None,
            "e164_number": E164,
            "status": status,
            "order_id": "order-1",
        }
        with (
            _patched(lookup, forget, record),
            patch(f"{TELNYX}.buy_number", AsyncMock(return_value=bought)),
        ):
            r = admin.post(
                PURCHASE, headers=H, json={"carrier": "telnyx", "e164_number": E164}
            )

        assert r.status_code == 201, r.text
        assert record.await_args.kwargs == {
            "project_id": project_id,
            "carrier": "telnyx",
            "e164_number": E164,
            "account": telnyx_account,
        }

    def test_a_refused_purchase_is_not_recorded(self, admin) -> None:
        from twilio.base.exceptions import TwilioRestException

        lookup, forget, record = _ledger(bought=False)
        gone = TwilioRestException(400, "uri", msg="not available", code=21422)
        with (
            _patched(lookup, forget, record),
            patch(f"{ADAPTER}.buy_number", AsyncMock(side_effect=gone)),
        ):
            r = admin.post(PURCHASE, headers=H, json={"e164_number": E164})

        assert r.status_code == 422, r.text
        record.assert_not_awaited()


class TestOnThePlatformsAccount:
    def test_twilio_releases_a_number_the_project_bought(
        self, admin, project_id
    ) -> None:
        lookup, forget, record = _ledger(bought=True)
        find, release, find_t, release_t = _carrier()
        with (
            _patched(lookup, forget, record),
            _carrier_patched(find, release, find_t, release_t),
        ):
            r = admin.post(RELEASE, headers=H, json=_release())

        assert r.status_code == 200, r.text
        release.assert_awaited_once_with(PN)
        # Asked about this project's purchases only.
        assert lookup.await_args.kwargs == {
            "project_id": project_id,
            "carrier": "twilio",
            "e164_number": E164,
        }
        assert forget.await_args.kwargs == lookup.await_args.kwargs

    def test_telnyx_releases_a_number_the_project_bought(
        self, admin, project_id, platform_telnyx
    ) -> None:
        lookup, forget, record = _ledger(bought=True)
        find, release, find_t, release_t = _carrier()
        with (
            _patched(lookup, forget, record),
            _carrier_patched(find, release, find_t, release_t),
        ):
            r = admin.post(RELEASE, headers=H, json=_release("telnyx"))

        assert r.status_code == 200, r.text
        release_t.assert_awaited_once_with("pn-777")
        assert lookup.await_args.kwargs == {
            "project_id": project_id,
            "carrier": "telnyx",
            "e164_number": E164,
        }
        forget.assert_awaited_once()

    @pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
    def test_a_number_the_project_did_not_buy_is_refused_untouched(
        self, admin, platform_telnyx, carrier
    ) -> None:
        lookup, forget, record = _ledger(bought=False)
        mocks = _carrier()
        with _patched(lookup, forget, record), _carrier_patched(*mocks):
            r = admin.post(RELEASE, headers=H, json=_release(carrier))

        assert r.status_code == 403, r.text
        body = r.json()
        assert body["code"] == "forbidden"
        assert body["details"] == {"account": "platform"}
        assert "POST /v1/phone-numbers/purchase" in body["error"]
        # Refused before the carrier is asked anything: not even whether the
        # account holds the number.
        for untouched in mocks:
            untouched.assert_not_awaited()
        forget.assert_not_awaited()
        assert AUDIT_HEADER not in r.headers

    def test_a_bound_number_is_still_a_409_first(self, admin, project_id) -> None:
        row = SimpleNamespace(id=uuid.uuid4(), project_id=project_id)
        lookup, forget, record = _ledger(bought=False)
        with (
            _patched(lookup, forget, record),
            patch(f"{REPO}.get_by_e164", AsyncMock(return_value=row)),
        ):
            r = admin.post(RELEASE, headers=H, json=_release())
        assert r.status_code == 409
        lookup.assert_not_awaited()


class TestOnTheProjectsOwnAccount:
    def test_twilio_with_an_account_pair_is_not_restricted(self, admin) -> None:
        lookup, forget, record = _ledger(bought=False)
        released: list[str] = []

        def adapter(account: Any) -> Any:
            return SimpleNamespace(
                find_number_sid=AsyncMock(return_value=PN),
                release_number=AsyncMock(side_effect=released.append),
            )

        with (
            _patched(lookup, forget, record),
            patch("turncall.services.twilio_accounts.TwilioAccount.adapter", adapter),
        ):
            r = admin.post(RELEASE, headers=H, json=_release(**OWN_PAIR))

        assert r.status_code == 200, r.text
        assert released == [PN]
        lookup.assert_not_awaited()
        forget.assert_awaited_once()

    def test_telnyx_with_the_projects_credential_is_not_restricted(
        self, admin, own_telnyx
    ) -> None:
        lookup, forget, record = _ledger(bought=False)
        find, release, find_t, release_t = _carrier()
        with (
            _patched(lookup, forget, record),
            _carrier_patched(find, release, find_t, release_t),
        ):
            r = admin.post(RELEASE, headers=H, json=_release("telnyx"))

        assert r.status_code == 200, r.text
        release_t.assert_awaited_once_with("pn-777")
        lookup.assert_not_awaited()
