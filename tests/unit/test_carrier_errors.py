"""One mapping of carrier refusals for every phone-number route (#307):
`api/v1/carrier_errors.py`, used by bind and by search, buy and release.

Where the request is the caller's (a number to buy or release, a search
filter), a Telnyx 400/404/422 is theirs to fix (422). Bind's Telnyx lookup is
TurnCall's own request, so the same refusal there stays Telnyx's (502), as it
always was. Telnyx not answering is a 502 everywhere, bind included.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from twilio.base.exceptions import TwilioRestException

from turncall.adapters.telephony.telnyx_adapter import TelnyxApiError
from turncall.api.errors import ApiError
from turncall.api.v1 import carrier_errors, phone_numbers

pytestmark = pytest.mark.unit

E164 = "+13125550100"


def _raised(call) -> ApiError:
    with pytest.raises(ApiError) as raised:
        call()
    return raised.value


class TestTelnyxRefusals:
    @pytest.mark.parametrize(
        ("status", "callers", "expected", "code"),
        [
            (401, True, 422, "validation_error"),
            (401, False, 422, "validation_error"),
            (404, True, 422, "validation_error"),
            (404, False, 502, "telnyx_error"),
            (422, False, 502, "telnyx_error"),
            (500, True, 502, "telnyx_error"),
        ],
    )
    def test_are_mapped_by_whose_request_it_was(
        self, status, callers, expected, code
    ) -> None:
        exc = TelnyxApiError(status, ["1"], "no")
        error = _raised(
            lambda: carrier_errors.telnyx_failed(exc, request_is_callers=callers)
        )
        assert (error.status_code, error.code) == (expected, code)

    def test_not_answering_is_a_502_that_can_say_what_to_check(self) -> None:
        error = _raised(
            lambda: carrier_errors.telnyx_unanswered(
                httpx.ReadTimeout("slow"), "it may have been placed"
            )
        )
        assert (error.status_code, error.code) == (502, "telnyx_error")
        assert (
            error.message
            == "Telnyx did not answer (ReadTimeout): it may have been placed"
        )


class TestTwilioRefusals:
    @pytest.mark.parametrize(
        ("status", "own", "expected"),
        [(401, True, 422), (401, False, 502), (500, True, 502)],
    )
    def test_are_mapped_by_whose_credentials(self, status, own, expected) -> None:
        exc = TwilioRestException(status, "/uri", msg="no")
        error = _raised(lambda: carrier_errors.twilio_failed(exc, own=own))
        assert error.status_code == expected


class TestBindOnTelnyx:
    @pytest.fixture
    def account(self, monkeypatch) -> SimpleNamespace:
        adapter = SimpleNamespace(find_number=AsyncMock())

        async def account_for(session, project_id):
            return SimpleNamespace(adapter=lambda: adapter)

        monkeypatch.setattr(phone_numbers, "project_telnyx_account", account_for)
        return adapter

    async def _bind_error(self) -> ApiError:
        with pytest.raises(ApiError) as raised:
            await phone_numbers._on_telnyx(AsyncMock(), uuid.uuid4(), E164, None)
        return raised.value

    async def test_telnyx_refusing_the_lookup_is_still_telnyxs_error(
        self, account
    ) -> None:
        account.find_number.side_effect = TelnyxApiError(404, ["1"], "no")
        error = await self._bind_error()
        assert (error.status_code, error.code) == (502, "telnyx_error")

    async def test_a_rejected_key_is_still_a_422(self, account) -> None:
        account.find_number.side_effect = TelnyxApiError(401, ["1"], "no")
        error = await self._bind_error()
        assert error.status_code == 422

    async def test_telnyx_not_answering_is_a_502_not_a_500(self, account) -> None:
        account.find_number.side_effect = httpx.ConnectTimeout("slow")
        error = await self._bind_error()
        assert (error.status_code, error.code) == (502, "telnyx_error")
