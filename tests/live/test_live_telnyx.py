"""Telnyx's REST against the real API (#218, #219).

What the unit suite takes on trust, because each is Telnyx's rule: the
request shapes routing a number sends — creating this deployment's Call
Control application with webhook format 2 and the account's outbound voice
profile, finding and correcting it, deleting it once nothing is on it — and
the phone number lookup a bind and a dial both start with.

An account with no numbers is enough: putting a number on the application is
the one step only a fake carrier covers. Everything this creates it deletes,
pass or fail. Needs TELNYX_API_KEY.
"""

import uuid

import pytest

from turncall.adapters.telephony.telnyx_adapter import (
    TelnyxAdapter,
    TelnyxApiError,
    application_name,
)

pytestmark = pytest.mark.live


async def test_the_application_is_created_corrected_and_deleted(telnyx_key) -> None:
    adapter = TelnyxAdapter(telnyx_key)
    host = f"turncall-live-{uuid.uuid4().hex[:12]}.example.com"
    url = f"https://{host}/webhooks/telnyx/voice"
    app_id = ""
    try:
        app_id = await adapter.ensure_application(url)
        created = await adapter._application(app_id)
        assert created is not None
        assert created["application_name"] == application_name(url)
        assert created["webhook_event_url"] == url
        assert created["webhook_api_version"] == "2"
        # An account with one outbound voice profile dials through it.
        profiles = await adapter._request("GET", "/outbound_voice_profiles")
        if len(profiles["data"]) == 1:
            outbound = created["outbound"]["outbound_voice_profile_id"]
            assert outbound == profiles["data"][0]["id"]

        # Found again by name, not duplicated; a changed URL is corrected.
        moved = f"https://{host}/v2/webhooks/telnyx/voice"
        assert await adapter.ensure_application(moved) == app_id
        assert (await adapter._application(app_id))["webhook_event_url"] == moved

        assert await adapter.delete_application_if_unused(app_id) is True
        assert await adapter._application(app_id) is None
        app_id = ""
    finally:
        if app_id:
            await adapter._request("DELETE", f"/call_control_applications/{app_id}")


async def test_someone_elses_application_is_never_deleted(telnyx_key) -> None:
    adapter = TelnyxAdapter(telnyx_key)
    created = await adapter._request(
        "POST",
        "/call_control_applications",
        body={
            "application_name": f"not-turncall-{uuid.uuid4().hex[:12]}",
            "webhook_event_url": "https://elsewhere.example.com/hook",
        },
    )
    app_id = created["data"]["id"]
    try:
        assert await adapter.delete_application_if_unused(app_id) is False
        assert await adapter._application(app_id) is not None
    finally:
        await adapter._request("DELETE", f"/call_control_applications/{app_id}")


async def test_a_number_the_account_does_not_hold_is_not_found(telnyx_key) -> None:
    assert await TelnyxAdapter(telnyx_key).find_number("+15550001234") is None


async def test_a_refused_key_says_so() -> None:
    with pytest.raises(TelnyxApiError) as exc:
        await TelnyxAdapter("KEY-not-a-real-one").find_number("+15550001234")
    assert exc.value.status == 401
