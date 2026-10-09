"""Routing a Telnyx number to TurnCall (#219): the adapter's REST, against an
in-memory Telnyx.

Telnyx routes a number by the Call Control application it is on, so binding is
"put it on this deployment's application", and unbinding "take it off, and
delete the application once nothing is on it". The account must be left as
tidy as it was found, and nobody else's connection touched. The request shapes
were checked against the real API by `tests/live/test_live_telnyx.py`.
"""

import json
from typing import Any

import httpx
import pytest

from turncall.adapters.telephony.telnyx_adapter import TelnyxAdapter, application_name

pytestmark = pytest.mark.unit

URL = "https://tc.example/webhooks/telnyx/voice"
OURS = application_name(URL)  # "TurnCall tc.example"


class FakeTelnyx:
    """Just the resources routing touches: numbers, applications, profiles."""

    def __init__(self, *, profiles: int = 1) -> None:
        self.numbers: dict[str, dict[str, Any]] = {}
        self.apps: dict[str, dict[str, Any]] = {}
        self.profiles = [{"id": f"ovp-{i}"} for i in range(profiles)]
        self.writes: list[str] = []
        self._next = 0

    def number(self, number_id: str, connection_id: str | None = None) -> None:
        self.numbers[number_id] = {
            "id": number_id,
            "phone_number": "+15551234567",
            "connection_id": connection_id,
        }

    def app(self, name: str, url: str = URL, version: str = "2") -> str:
        self._next += 1
        app_id = f"app-{self._next}"
        self.apps[app_id] = {
            "id": app_id,
            "application_name": name,
            "webhook_event_url": url,
            "webhook_api_version": version,
        }
        return app_id

    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.removeprefix("/v2/").split("/")
        body = json.loads(request.content) if request.content else {}
        if request.method != "GET":
            self.writes.append(f"{request.method} {'/'.join(parts)}")
        if parts[0] == "phone_numbers":
            if len(parts) == 2:
                number = self.numbers[parts[1]]
                number.update(body)
                return httpx.Response(200, json={"data": number})
            on = request.url.params.get("filter[connection_id]")
            found = [n for n in self.numbers.values() if n["connection_id"] == on]
            return httpx.Response(200, json={"data": found})
        if parts[0] == "outbound_voice_profiles":
            return httpx.Response(200, json={"data": self.profiles})
        if len(parts) == 2:
            app = self.apps.get(parts[1])
            if app is None:
                return httpx.Response(404, json={"errors": [{"code": "10005"}]})
            if request.method == "DELETE":
                del self.apps[parts[1]]
            app.update(body)
            return httpx.Response(200, json={"data": app})
        if request.method == "POST":
            app_id = self.app(body["application_name"], body["webhook_event_url"])
            self.apps[app_id].update(body)
            return httpx.Response(201, json={"data": self.apps[app_id]})
        name = request.url.params["filter[application_name][contains]"]
        found = [a for a in self.apps.values() if name in a["application_name"]]
        return httpx.Response(200, json={"data": found})

    def adapter(self) -> TelnyxAdapter:
        return TelnyxAdapter("KEY", transport=httpx.MockTransport(self.handle))


async def test_binding_creates_the_application_and_puts_the_number_on_it() -> None:
    telnyx = FakeTelnyx()
    telnyx.number("n1")
    await telnyx.adapter().configure_number_webhook("n1", URL, "")

    [app] = telnyx.apps.values()
    assert app["application_name"] == "TurnCall tc.example"
    assert app["webhook_event_url"] == URL
    # Telnyx's default is "1", a shape the webhook handler cannot read.
    assert app["webhook_api_version"] == "2"
    # The account's only outbound voice profile, or the app could not dial.
    assert app["outbound"] == {"outbound_voice_profile_id": "ovp-0"}
    assert telnyx.numbers["n1"]["connection_id"] == app["id"]


async def test_with_several_profiles_none_is_chosen() -> None:
    telnyx = FakeTelnyx(profiles=2)
    telnyx.number("n1")
    await telnyx.adapter().configure_number_webhook("n1", URL, "")
    [app] = telnyx.apps.values()
    assert "outbound" not in app


async def test_a_second_number_shares_the_application() -> None:
    telnyx = FakeTelnyx()
    app_id = telnyx.app(OURS)
    telnyx.number("n1", app_id)
    telnyx.number("n2")
    await telnyx.adapter().configure_number_webhook("n2", URL, "")
    assert list(telnyx.apps) == [app_id]
    assert telnyx.writes == ["PATCH phone_numbers/n2"]


async def test_rebinding_a_routed_number_writes_nothing() -> None:
    telnyx = FakeTelnyx()
    app_id = telnyx.app(OURS)
    telnyx.number("n1", app_id)
    await telnyx.adapter().configure_number_webhook("n1", URL, "")
    assert telnyx.writes == []


async def test_an_application_left_on_webhook_format_1_is_corrected() -> None:
    telnyx = FakeTelnyx()
    app_id = telnyx.app(OURS, version="1")
    telnyx.number("n1", app_id)
    await telnyx.adapter().configure_number_webhook("n1", URL, "")
    assert telnyx.apps[app_id]["webhook_api_version"] == "2"
    assert telnyx.writes == [f"PATCH call_control_applications/{app_id}"]


async def test_a_new_host_moves_the_number_and_deletes_the_emptied_app() -> None:
    """PUBLIC_BASE_URL changed (ngrok rotated): the old host's application
    would otherwise be left behind with nothing on it."""
    telnyx = FakeTelnyx()
    old = telnyx.app(
        "TurnCall old.example", "https://old.example/webhooks/telnyx/voice"
    )
    telnyx.number("n1", old)
    await telnyx.adapter().configure_number_webhook("n1", URL, "")
    [app] = telnyx.apps.values()
    assert app["application_name"] == OURS
    assert telnyx.numbers["n1"]["connection_id"] == app["id"]


async def test_moving_off_someone_elses_connection_leaves_it_alone() -> None:
    telnyx = FakeTelnyx()
    theirs = telnyx.app("Customer IVR", "https://ivr.example/hook")
    telnyx.number("n1", theirs)
    await telnyx.adapter().configure_number_webhook("n1", URL, "")
    assert theirs in telnyx.apps


async def test_unbinding_the_last_number_leaves_the_account_as_it_was() -> None:
    telnyx = FakeTelnyx()
    app_id = telnyx.app(OURS)
    telnyx.number("n1", app_id)
    await telnyx.adapter().configure_number_webhook("n1", "", "")
    assert telnyx.numbers["n1"]["connection_id"] is None
    assert telnyx.apps == {}


async def test_unbinding_one_of_two_keeps_the_application() -> None:
    telnyx = FakeTelnyx()
    app_id = telnyx.app(OURS)
    telnyx.number("n1", app_id)
    telnyx.number("n2", app_id)
    await telnyx.adapter().configure_number_webhook("n1", "", "")
    assert telnyx.numbers["n1"]["connection_id"] is None
    assert list(telnyx.apps) == [app_id]


async def test_unbinding_a_number_someone_moved_away_touches_nothing() -> None:
    """Routed elsewhere since it was bound: not ours to undo."""
    telnyx = FakeTelnyx()
    theirs = telnyx.app("Customer IVR", "https://ivr.example/hook")
    telnyx.number("n1", theirs)
    await telnyx.adapter().configure_number_webhook("n1", "", "")
    assert telnyx.writes == []
