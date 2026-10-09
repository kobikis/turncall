"""A bridge ARI refuses ends the call instead of stranding it (#209).

The edge bridges the caller and the media channel once the media channel
answers. Every event handler runs as a bare task, and the bridge was the one
step with no recovery: if it raised — the caller gone a moment earlier, a
network blip — the caller and the pipeline were both left up and unbridged,
each hearing silence, the live-call slot held, until
`max_call_duration_seconds`.
"""

from __future__ import annotations

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app

pytestmark = pytest.mark.integration

AGENT = {**HARNESS_AGENT, "first_message": "Hello from Asterisk."}


async def test_a_failed_bridge_hangs_both_legs_up(monkeypatch) -> None:
    from turncall.adapters.telephony.asterisk_adapter import AsteriskAdapter

    async def refused(self, bridge_id: str, channel_ids: list[str]) -> None:
        raise RuntimeError("Channel not in Stasis application")

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_asterisk_number(await app.create_agent(AGENT))
        monkeypatch.setattr(AsteriskAdapter, "bridge", refused)
        call = await app.asterisk.ring("+15550001234", number, until_bridged=False)
        ended = (await app.sink.wait_for("call.ended", timeout=15))["payload"]

    hung_up = [
        a for a in app.carrier.actions_of("hang_up") if a.params["call_sid"] == call.call_sid
    ]
    assert hung_up, "the caller's channel was left up"
    assert ended["status"] in {"failed", "completed"}
