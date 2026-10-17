"""Retention, end to end (#316, ADR-0024).

A project sets how long its calls' recordings, transcripts and events are
kept, an agent overrides it, and `call.ended` states what applies. Once the
janitor has swept past it, the reads return nothing of what expired, and the
call itself is still there. Driven through Seam A (the real app, stub
providers, a fake carrier); the janitor is run in the test with its clock
moved forward, as `test_retention_janitor.py` runs it against the database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.harness import (
    HARNESS_AGENT,
    Harness,
    Script,
    ToolCall,
    place_inbound_call,
    run_app,
)
from turncall.adapters.storage import LocalStorageAdapter
from turncall.config.settings import get_settings
from turncall.services import retention
from turncall.storage.database import create_engine, create_session_factory

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
END_CALL = {
    "name": "end_call",
    "description": "End the call",
    "parameters_schema": {"type": "object", "properties": {}},
}
AGENT = {**HARNESS_AGENT, "first_message": "Hello.", "tools": [END_CALL]}
SCRIPT = Script(
    caller=("Please end the call.",),
    agent=(ToolCall("end_call", {"reason": "caller asked"}), "Goodbye!"),
)
KEPT_FOR_GOOD = {"recordings_days": None, "transcripts_days": None, "events_days": None}


async def _call(app: Harness, agent: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A call the agent ends; its id and its `call.ended` payload."""
    number = await app.bind_number(await app.create_agent(agent))
    call = await place_inbound_call(
        app.carrier, app.api, from_number=CALLER, to_number=number
    )
    await call.wait_for_agent_speech(1)
    await call.say(400)
    await call.closed.wait()
    await call.finish()
    return call.call_id, (await app.sink.wait_for("call.ended"))["payload"]


async def _sweep(days: int) -> retention.Swept:
    """The janitor, `days` from now, on the app's database and storage."""
    settings = get_settings()
    engine = create_engine(settings.database)
    try:
        return await retention.sweep(
            create_session_factory(engine),
            LocalStorageAdapter(settings.storage.local_path),
            now=datetime.now(UTC) + timedelta(days=days),
        )
    finally:
        await engine.dispose()


async def _reads(app: Harness, call_id: str) -> dict[str, Any]:
    base = f"/v1/calls/{call_id}"
    return {
        "call": (await app.api.get(base)).json()["data"],
        "events": [
            e["event_type"]
            for e in (await app.api.get(f"{base}/events")).json()["data"]
        ],
        "transcript": (await app.api.get(f"{base}/transcript")).json()["data"],
        "recording": (await app.api.get(f"{base}/recording")).status_code,
        "analysis": (await app.api.get(f"{base}/analysis")).json()["data"],
    }


async def test_what_expired_reads_as_nothing_and_the_call_stays(monkeypatch) -> None:
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        put = await app.api.put(
            f"/v1/projects/{app.project_id}/retention",
            json={"recordings_days": 30, "transcripts_days": 30, "events_days": 90},
        )
        assert put.status_code == 200, put.text
        # The agent keeps events 60 days, not the project's 90.
        call_id, ended = await _call(app, {**AGENT, "retention": {"events_days": 60}})
        before = await _reads(app, call_id)
        recording = (
            Path(get_settings().storage.local_path) / f"recordings/{call_id}.wav"
        )
        recorded = recording.exists()

        await _sweep(29)
        unchanged = await _reads(app, call_id)
        await _sweep(31)
        expired = await _reads(app, call_id)
        rerun = await app.api.post(f"/v1/calls/{call_id}/analysis/rerun")
        await _sweep(61)
        gone = await _reads(app, call_id)

    # call.ended states what applies to this call.
    assert ended["retention"] == {
        "recordings_days": 30,
        "transcripts_days": 30,
        "events_days": 60,
    }
    assert put.json()["data"]["retention"] == {
        "recordings_days": 30,
        "transcripts_days": 30,
        "events_days": 90,
    }
    assert recorded and before["recording"] == 200
    assert (
        before["transcript"]["entries"] and before["transcript"]["expired_at"] is None
    )
    assert {"transcript.final", "tool.result"} <= set(before["events"])
    assert unchanged == before

    # Past 30 days: no recording and no transcript, on any read.
    call = expired["call"]
    assert not recording.exists()
    assert expired["recording"] == 404
    assert call["recording_url"] is None and call["recording_expired_at"]
    assert expired["transcript"]["entries"] == []
    assert expired["transcript"]["expired_at"] == call["transcript_expired_at"]
    assert not {"transcript.final", "tool.result"} & set(expired["events"])
    assert "call.started" in expired["events"]
    assert call["analysis"] is None
    assert expired["analysis"]["status"] == "expired"
    assert rerun.status_code == 409
    assert call["events_expired_at"] is None

    # Past the agent's 60: the event log is empty, and the call is still
    # there, saying how it ended.
    call = gone["call"]
    assert gone["events"] == []
    assert call["events_expired_at"]
    assert (
        call["ended_reason"]
        == before["call"]["ended_reason"]
        == ("assistant_ended_call")
    )
    assert call["id"] == call_id and call["from_number"] == CALLER
    assert call["status"] == "completed" and call["duration_ms"] is not None


async def test_unset_keeps_everything_and_says_so(monkeypatch) -> None:
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        project = (await app.api.get(f"/v1/projects/{app.project_id}")).json()
        call_id, ended = await _call(app, AGENT)
        before = await _reads(app, call_id)
        await _sweep(36500)
        after = await _reads(app, call_id)

    assert project["data"]["retention"] == KEPT_FOR_GOOD
    assert ended["retention"] == KEPT_FOR_GOOD
    assert after == before
    assert before["transcript"]["entries"] and before["recording"] == 200


async def test_only_an_admin_key_sets_retention(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        url = f"/v1/projects/{app.project_id}/retention"
        refused = {}
        for role in ("developer", "viewer"):
            key = await app.api.post(
                "/v1/api-keys/authenticated", json={"name": role, "role": role}
            )
            assert key.status_code == 201, key.text
            response = await app.api.put(
                url,
                json={"transcripts_days": 30},
                headers={"Authorization": f"Bearer {key.json()['data']['raw_key']}"},
            )
            refused[role] = response.status_code
        invalid = [
            (await app.api.put(url, json=body)).status_code
            for body in ({"transcripts_days": 0}, {"days": 30})
        ]
        await app.api.put(url, json={"transcripts_days": 30, "events_days": 90})
        # A PUT replaces all three: what it leaves out is kept for good again.
        replaced = await app.api.put(url, json={"recordings_days": 7})
        other = await app.api.put(
            "/v1/projects/00000000-0000-0000-0000-000000000001/retention",
            json={"recordings_days": 7},
        )
        agent = await app.api.post(
            "/v1/agents",
            json={"name": "bad", "config": {**AGENT, "retention": {"events_days": 0}}},
        )

    assert refused == {"developer": 403, "viewer": 403}
    assert invalid == [422, 422]
    assert replaced.json()["data"]["retention"] == {
        **KEPT_FOR_GOOD,
        "recordings_days": 7,
    }
    assert other.status_code == 403
    assert agent.status_code == 422


async def test_only_an_admin_key_changes_what_an_agent_keeps(monkeypatch) -> None:
    """An agent's `retention` overrides the project's, which only an admin
    sets: a developer key may carry it over unchanged, never change what
    applies to the agent's calls, at create, update, publish or rollback."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        key = await app.api.post(
            "/v1/api-keys/authenticated", json={"name": "dev", "role": "developer"}
        )
        dev = {"Authorization": f"Bearer {key.json()['data']['raw_key']}"}
        kept_30 = {**AGENT, "retention": {"transcripts_days": 30}}
        kept_1 = {**AGENT, "retention": {"transcripts_days": 1}}

        async def agent(config: dict[str, Any], headers: Any = None) -> Any:
            return await app.api.post(
                "/v1/agents",
                json={"name": "support", "config": config},
                headers=headers or {},
            )

        refused_create = await agent(kept_30, dev)
        listed = (await app.api.get("/v1/agents")).json()["data"]
        plain = await agent(AGENT, dev)  # no retention: nothing changes
        v2 = await agent(kept_30)  # admin: v2 is the newest, and applies
        v2_id = v2.json()["data"]["id"]
        await app.api.post(f"/v1/agents/{v2_id}/publish")
        # A developer's v3 carries v2's retention over: allowed. Its own
        # retention changed is refused, on update and on publish.
        v3 = await agent(kept_30, dev)
        v3_id = v3.json()["data"]["id"]
        refused_update = await app.api.put(
            f"/v1/agents/{v3_id}", json={"config": kept_1}, headers=dev
        )
        # The admin changes the draft; the developer may not publish it.
        await app.api.put(f"/v1/agents/{v3_id}", json={"config": kept_1})
        refused_publish = await app.api.post(f"/v1/agents/{v3_id}/publish", headers=dev)
        published = await app.api.post(f"/v1/agents/{v3_id}/publish")
        refused_rollback = await app.api.post(
            f"/v1/agents/{v2_id}/rollback", headers=dev
        )
        v3_read = (await app.api.get(f"/v1/agents/{v3_id}")).json()["data"]

    assert refused_create.status_code == 403, refused_create.text
    error = refused_create.json()
    assert "admin key" in error["error"] and error["code"] == "forbidden"
    assert error["details"]["requested"]["transcripts_days"] == 30
    assert listed == []  # the refused create was rolled back
    assert plain.status_code == 201
    assert v2.status_code == 201 and v3.status_code == 201, v3.text
    assert refused_update.status_code == 403
    assert refused_publish.status_code == 403
    assert published.status_code == 200
    assert refused_rollback.status_code == 403
    assert v3_read["state"] == "published"
