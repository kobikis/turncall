"""Searching and buying numbers on a Twilio account (#319, ADR-0025).

`TwilioAdapter.search_numbers` and `.buy_number` are Twilio's own, not
`TelephonyAdapter` actions (Asterisk owns no store of numbers; ADR-0022).
They run the real Twilio SDK against an in-memory HTTP client, so what is
checked is the REST request Twilio would receive and what TurnCall makes of
its answer.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import pytest
from twilio.base.exceptions import TwilioRestException
from twilio.http import HttpClient
from twilio.http.response import Response
from twilio.rest import Client

from turncall.adapters.telephony.twilio_adapter import TwilioAdapter

pytestmark = pytest.mark.unit

SID = "AC" + "a" * 32
API = f"/2010-04-01/Accounts/{SID}"


class FakeTwilioHttp(HttpClient):
    """Twilio's REST API, in memory: answers each request with the next
    queued (status, body) and keeps what it was asked."""

    def __init__(self, *answers: tuple[int, dict[str, Any]]) -> None:
        super().__init__(logger=None, is_async=False)  # type: ignore[arg-type]
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def request(self, method, uri, params=None, data=None, **_: Any) -> Response:
        self.requests.append(
            {
                "method": method,
                "path": urlparse(uri).path,
                "params": dict(params or {}),
                "data": dict(data or {}),
            }
        )
        status, body = self.answers.pop(0)
        return Response(status, json.dumps(body))


def _adapter(http: FakeTwilioHttp) -> TwilioAdapter:
    adapter = TwilioAdapter(SID, "token")
    adapter._client = Client(SID, "token", http_client=http)
    return adapter


def _available(e164: str, **kw: Any) -> dict[str, Any]:
    return {
        "phone_number": e164,
        "friendly_name": kw.get("friendly_name", "(312) 555-0100"),
        "locality": "Chicago",
        "region": "IL",
        "postal_code": "60601",
        "iso_country": "US",
        # Twilio's own casing: SMS and MMS upper-case.
        "capabilities": {"voice": True, "SMS": True, "MMS": False, "fax": False},
        "lata": "358",
        "rate_center": "CHICAGO",
        "address_requirements": "none",
        "beta": False,
    }


def _page(key: str, *numbers: dict[str, Any]) -> dict[str, Any]:
    return {key: list(numbers), "next_page_uri": None, "uri": "", "page": 0}


class TestSearch:
    async def test_asks_for_voice_numbers_matching_the_filters(self) -> None:
        http = FakeTwilioHttp(
            (200, _page("available_phone_numbers", _available("+13125550100")))
        )

        found = await _adapter(http).search_numbers(
            "US", area_code="312", contains="555", limit=5
        )

        [request] = http.requests
        assert request["method"] == "GET"
        assert request["path"] == f"{API}/AvailablePhoneNumbers/US/Local.json"
        assert request["params"] == {
            "AreaCode": 312,
            "Contains": "555",
            "VoiceEnabled": "true",
            "PageSize": 5,
        }
        assert found == [
            {
                "e164_number": "+13125550100",
                "friendly_name": "(312) 555-0100",
                "country": "US",
                "region": "IL",
                "locality": "Chicago",
                "postal_code": "60601",
                "number_type": "local",
                "capabilities": {"voice": True, "sms": True, "mms": False},
            }
        ]

    @pytest.mark.parametrize(
        ("number_type", "resource"),
        [("local", "Local"), ("toll_free", "TollFree"), ("mobile", "Mobile")],
    )
    async def test_each_type_has_its_own_list(
        self, number_type: str, resource: str
    ) -> None:
        http = FakeTwilioHttp((200, _page("available_phone_numbers")))

        found = await _adapter(http).search_numbers("GB", number_type=number_type)

        assert found == []
        [request] = http.requests
        assert request["path"] == f"{API}/AvailablePhoneNumbers/GB/{resource}.json"
        # No filter but voice: what is unset is not sent.
        assert set(request["params"]) == {"VoiceEnabled", "PageSize"}

    async def test_a_type_twilio_does_not_sell_there_raises(self) -> None:
        http = FakeTwilioHttp((404, {"code": 20404, "message": "Not Found"}))

        with pytest.raises(TwilioRestException) as raised:
            await _adapter(http).search_numbers("US", number_type="mobile")

        assert raised.value.status == 404

    async def test_an_unknown_type_is_refused_before_twilio(self) -> None:
        http = FakeTwilioHttp()
        with pytest.raises(ValueError, match="number type"):
            await _adapter(http).search_numbers("US", number_type="national")
        assert http.requests == []


class TestBuy:
    async def test_buys_the_number_and_returns_what_bind_needs(self) -> None:
        bought = {
            "sid": "PN" + "b" * 32,
            "account_sid": SID,
            "phone_number": "+13125550100",
            "friendly_name": "(312) 555-0100",
        }
        http = FakeTwilioHttp((201, bought))

        number = await _adapter(http).buy_number("+13125550100")

        [request] = http.requests
        assert request["method"] == "POST"
        assert request["path"] == f"{API}/IncomingPhoneNumbers.json"
        assert request["data"] == {"PhoneNumber": "+13125550100"}
        assert number == {
            "number_sid": "PN" + "b" * 32,
            "e164_number": "+13125550100",
            "friendly_name": "(312) 555-0100",
            "status": "active",
        }

    async def test_a_number_no_longer_available_raises(self) -> None:
        http = FakeTwilioHttp(
            (
                400,
                {
                    "code": 21422,
                    "message": "Phone number is not available",
                    "status": 400,
                },
            )
        )

        with pytest.raises(TwilioRestException) as raised:
            await _adapter(http).buy_number("+13125550100")

        assert raised.value.status == 400
        assert raised.value.code == 21422
