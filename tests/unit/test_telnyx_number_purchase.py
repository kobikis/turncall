"""Searching, buying and releasing numbers on Telnyx (#320, #321,
ADR-0025): the adapter's REST, against an in-memory Telnyx.

A search is `GET /available_phone_numbers`, turned into the carrier-neutral
shape a Twilio search returns. A purchase is a number order, which Telnyx
finishes asynchronously, and whose ids are not the number's: the adapter
looks at the order again for a few seconds, then finds the bought number on
the account for the id bind stores. An order still pending is reported as
pending, never as a failure: the money is spent. A release is
`DELETE /phone_numbers/{id}`, by the phone number's id. The route halves are
`test_phone_number_purchase.py` and `test_phone_number_release.py`; the
end-to-end halves `tests/integration/test_harness_telnyx_number_purchase.py`
and `test_harness_number_release.py`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from turncall.adapters.telephony import telnyx_adapter
from turncall.adapters.telephony.telnyx_adapter import (
    TelnyxAdapter,
    TelnyxApiError,
    TelnyxOrderFailed,
)

pytestmark = pytest.mark.unit

E164 = "+13125550100"

AVAILABLE = {
    "record_type": "available_phone_number",
    "phone_number": E164,
    "vanity_format": "",
    "best_effort": False,
    "quickship": True,
    "reservable": True,
    "region_information": [
        {"region_type": "country_code", "region_name": "US"},
        {"region_type": "rate_center", "region_name": "CHICAGO ZONE 1"},
        {"region_type": "state", "region_name": "IL"},
        {"region_type": "location", "region_name": "Chicago"},
    ],
    "cost_information": {
        "upfront_cost": "1.00",
        "monthly_cost": "1.00",
        "currency": "USD",
    },
    "features": [{"name": "voice"}, {"name": "sms"}, {"name": "emergency"}],
}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch) -> None:
    """Look at a pending order three times, without sleeping between."""
    monkeypatch.setattr(telnyx_adapter, "ORDER_POLL_DELAYS", (0, 0, 0))


class FakeTelnyx:
    """Just what a search and a purchase touch. `order_states` is the
    order's status at each look: the create, then each poll."""

    def __init__(
        self,
        *,
        available: list[dict[str, Any]] | None = None,
        order_states: list[str] = ("success",),  # type: ignore[assignment]
        requirements_met: bool = True,
    ) -> None:
        self.available = available if available is not None else [AVAILABLE]
        self.order_states = list(order_states)
        self.requirements_met = requirements_met
        self.numbers: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.refuse: dict[str, httpx.Response] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path.removeprefix('/v2')}"
        for prefix, response in self.refuse.items():
            if key.startswith(prefix):
                return response
        if key == "GET /available_phone_numbers":
            return httpx.Response(200, json={"data": self.available})
        if key == "POST /number_orders":
            body = json.loads(request.content)
            self.ordered = body["phone_numbers"][0]["phone_number"]
            return httpx.Response(200, json={"data": self._order()})
        if key == "GET /number_orders/order-1":
            return httpx.Response(200, json={"data": self._order()})
        if key == "GET /phone_numbers":
            digits = request.url.params["filter[phone_number]"]
            found = [n for n in self.numbers if digits in n["phone_number"]]
            return httpx.Response(200, json={"data": found})
        if key.startswith("DELETE /phone_numbers/"):
            number_id = key.removeprefix("DELETE /phone_numbers/")
            gone = [n for n in self.numbers if n["id"] == number_id]
            if not gone:
                return httpx.Response(
                    404, json={"errors": [{"code": "10005", "detail": "Not found"}]}
                )
            self.numbers.remove(gone[0])
            return httpx.Response(200, json={"data": gone[0]})
        raise AssertionError(f"unexpected {key}")

    def _order(self) -> dict[str, Any]:
        # The last state sticks.
        states = self.order_states
        status = states.pop(0) if len(states) > 1 else states[0]
        if status == "success" and not self.numbers:
            self.numbers.append({"id": "pn-777", "phone_number": self.ordered})
        return {
            "id": "order-1",
            "record_type": "number_order",
            "status": status,
            "requirements_met": self.requirements_met,
            "phone_numbers": [
                {"id": "line-9", "phone_number": self.ordered, "status": status}
            ],
        }

    def adapter(self) -> TelnyxAdapter:
        return TelnyxAdapter("KEY", transport=httpx.MockTransport(self.handle))

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path.removeprefix('/v2')}" for r in self.requests]


class TestSearch:
    async def test_asks_for_voice_numbers_matching_the_filters_exactly(self) -> None:
        telnyx = FakeTelnyx()
        await telnyx.adapter().search_numbers(
            "US", number_type="toll_free", area_code="312", contains="555", limit=5
        )
        [request] = telnyx.requests
        assert request.url.path == "/v2/available_phone_numbers"
        params = request.url.params
        assert params["filter[country_code]"] == "US"
        assert params["filter[phone_number_type]"] == "toll_free"
        assert params["filter[national_destination_code]"] == "312"
        assert params["filter[phone_number][contains]"] == "555"
        assert params.get_list("filter[features][]") == ["voice"]
        assert params["filter[best_effort]"] == "false"
        assert params["filter[limit]"] == "5"
        assert request.headers["Authorization"] == "Bearer KEY"

    async def test_without_filters_none_is_sent(self) -> None:
        telnyx = FakeTelnyx()
        await telnyx.adapter().search_numbers("GB")
        params = telnyx.requests[0].url.params
        assert "filter[national_destination_code]" not in params
        assert "filter[phone_number][contains]" not in params
        assert params["filter[phone_number_type]"] == "local"
        assert params["filter[limit]"] == "20"

    async def test_a_number_comes_back_in_the_carrier_neutral_shape(self) -> None:
        [number] = await FakeTelnyx().adapter().search_numbers("US")
        assert number == {
            "e164_number": E164,
            "friendly_name": None,
            "country": "US",
            "region": "IL",
            "locality": "Chicago",
            "postal_code": None,
            "number_type": "local",
            "capabilities": {"voice": True, "sms": True, "mms": False},
            "upfront_cost": "1.00",
            "monthly_cost": "1.00",
            "currency": "USD",
        }

    async def test_best_effort_and_voiceless_numbers_are_left_out(self) -> None:
        padded = {**AVAILABLE, "phone_number": "+13125550101", "best_effort": True}
        voiceless = {
            **AVAILABLE,
            "phone_number": "+13125550102",
            "features": [{"name": "sms"}],
        }
        telnyx = FakeTelnyx(available=[AVAILABLE, padded, voiceless])
        found = await telnyx.adapter().search_numbers("US")
        assert [n["e164_number"] for n in found] == [E164]

    async def test_a_refused_search_raises(self) -> None:
        telnyx = FakeTelnyx()
        telnyx.refuse["GET /available_phone_numbers"] = httpx.Response(
            400, json={"errors": [{"code": "10015", "detail": "Bad country"}]}
        )
        with pytest.raises(TelnyxApiError) as raised:
            await telnyx.adapter().search_numbers("ZZ")
        assert raised.value.status == 400
        assert raised.value.codes == ["10015"]


class TestBuy:
    async def test_orders_the_number_and_answers_with_its_phone_number_id(
        self,
    ) -> None:
        telnyx = FakeTelnyx(order_states=["success"])
        bought = await telnyx.adapter().buy_number(E164)

        assert json.loads(telnyx.requests[0].content) == {
            "phone_numbers": [{"phone_number": E164}]
        }
        # The number's id, found on the account: not the order's, nor its line's.
        assert bought == {
            "number_sid": "pn-777",
            "e164_number": E164,
            "friendly_name": None,
            "status": "active",
            "order_id": "order-1",
        }
        assert telnyx.paths() == ["POST /number_orders", "GET /phone_numbers"]
        assert telnyx.requests[1].url.params["filter[phone_number]"] == E164[1:]

    async def test_a_pending_order_is_looked_at_again_until_it_succeeds(
        self,
    ) -> None:
        telnyx = FakeTelnyx(order_states=["pending", "pending", "success"])
        bought = await telnyx.adapter().buy_number(E164)

        assert bought["status"] == "active"
        assert bought["number_sid"] == "pn-777"
        assert telnyx.paths() == [
            "POST /number_orders",
            "GET /number_orders/order-1",
            "GET /number_orders/order-1",
            "GET /phone_numbers",
        ]

    async def test_an_order_still_pending_is_reported_pending(self) -> None:
        telnyx = FakeTelnyx(order_states=["pending"])
        bought = await telnyx.adapter().buy_number(E164)

        assert bought["status"] == "pending"
        assert bought["number_sid"] is None
        assert bought["order_id"] == "order-1"
        # Three looks, then the request answers rather than hang on.
        assert telnyx.paths().count("GET /number_orders/order-1") == 3
        assert "GET /phone_numbers" not in telnyx.paths()

    async def test_an_order_awaiting_requirements_is_not_waited_on(self) -> None:
        telnyx = FakeTelnyx(order_states=["pending"], requirements_met=False)
        bought = await telnyx.adapter().buy_number(E164)
        assert bought["status"] == "pending"
        assert telnyx.paths() == ["POST /number_orders"]

    async def test_a_failed_order_raises(self) -> None:
        telnyx = FakeTelnyx(order_states=["pending", "failure"])
        with pytest.raises(TelnyxOrderFailed) as raised:
            await telnyx.adapter().buy_number(E164)
        assert raised.value.order_id == "order-1"

    async def test_a_refused_order_raises(self) -> None:
        telnyx = FakeTelnyx()
        telnyx.refuse["POST /number_orders"] = httpx.Response(
            422, json={"errors": [{"code": "85001", "detail": "Not available"}]}
        )
        with pytest.raises(TelnyxApiError) as raised:
            await telnyx.adapter().buy_number(E164)
        assert raised.value.status == 422

    async def test_once_ordered_a_failed_look_is_pending_not_an_error(self) -> None:
        """The order is placed and billed: a poll Telnyx fails must not turn
        the purchase into an error the caller would retry."""
        telnyx = FakeTelnyx(order_states=["pending"])
        telnyx.refuse["GET /number_orders"] = httpx.Response(503, json={})
        bought = await telnyx.adapter().buy_number(E164)
        assert bought["status"] == "pending"
        assert bought["order_id"] == "order-1"

    async def test_a_failed_lookup_after_success_is_pending(self) -> None:
        telnyx = FakeTelnyx(order_states=["success"])
        telnyx.refuse["GET /phone_numbers"] = httpx.Response(500, json={})
        bought = await telnyx.adapter().buy_number(E164)
        assert bought["status"] == "pending"
        assert bought["number_sid"] is None


class TestRelease:
    async def test_deletes_the_number_by_its_phone_number_id(self) -> None:
        telnyx = FakeTelnyx()
        telnyx.numbers.append({"id": "pn-777", "phone_number": E164})

        assert await telnyx.adapter().release_number("pn-777") is None

        [request] = telnyx.requests
        assert request.method == "DELETE"
        assert request.url.path == "/v2/phone_numbers/pn-777"
        assert request.headers["Authorization"] == "Bearer KEY"
        assert telnyx.numbers == []

    async def test_the_id_is_escaped_into_the_path(self) -> None:
        telnyx = FakeTelnyx()
        with pytest.raises(TelnyxApiError):
            await telnyx.adapter().release_number("a/b")
        assert telnyx.requests[0].url.raw_path == b"/v2/phone_numbers/a%2Fb"

    async def test_a_number_not_on_the_account_raises_404(self) -> None:
        telnyx = FakeTelnyx()
        with pytest.raises(TelnyxApiError) as raised:
            await telnyx.adapter().release_number("pn-404")
        assert raised.value.status == 404
        assert raised.value.codes == ["10005"]
