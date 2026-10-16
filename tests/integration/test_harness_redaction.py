"""Redaction, end to end (#313, ADR-0024).

A project that turns redaction on gets its callers' numbers and emails
replaced in everything it stores and sends — the transcript read, the call's
event log, every webhook — while the agent, during the call, works with the
raw values: the model is given them, the operator of a warm transfer is
briefed with them, and post-call analysis and takeaways read them. A project
with redaction off is untouched and never loads the detector.

Driven through Seam A: the real app, stub providers, a fake carrier, a signed
webhook subscriber. The detector is the real one, Presidio over spaCy's small
model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.harness import (
    HARNESS_AGENT,
    Given,
    Harness,
    Script,
    ToolCall,
    place_inbound_call,
    run_app,
)

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
NUMBER = "415 555 0132"
EMAIL = "jane.doe@example.com"
SAID = f"My number is {NUMBER} and my email is {EMAIL}."
OPERATOR = "+15559990000"

END_CALL = {
    "name": "end_call",
    "description": "End the call",
    "parameters_schema": {"type": "object", "properties": {}},
}
AGENT = {**HARNESS_AGENT, "first_message": "Hello.", "tools": [END_CALL]}


def _remembering(given: list[Given], reply: Any):
    """An agent reply that keeps what the model was given."""

    def answer(context: Given) -> Any:
        given.append(context)
        return reply

    return answer


def _user_text(given: list[Given]) -> str:
    return " ".join(
        str(m.get("content"))
        for context in given
        for m in context
        if m["role"] == "user"
    )


def _raw_in(document: Any) -> list[str]:
    """Which of the caller's raw values `document` still carries."""
    text = json.dumps(document)
    return [raw for raw in (NUMBER, EMAIL, CALLER) if raw in text]


async def _redact(app: Harness, **setting: Any) -> dict[str, Any]:
    response = await app.api.put(
        f"/v1/projects/{app.project_id}/redaction", json=setting
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["redaction"]


async def _call(app: Harness, agent: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The caller rings, says SAID, and the agent hangs up; returns the call
    and its `call.ended` webhook."""
    number = await app.bind_number(await app.create_agent(agent))
    call = await place_inbound_call(
        app.carrier, app.api, from_number=CALLER, to_number=number
    )
    await call.wait_for_agent_speech(1)  # the greeting
    await call.say(400)
    await call.closed.wait()
    await call.finish()
    return call, await app.sink.wait_for("call.ended")


async def test_what_is_stored_and_sent_is_redacted_but_the_model_hears_it(
    monkeypatch,
) -> None:
    given: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            _remembering(given, (f"Got it, {NUMBER}.", ToolCall("end_call"))),
            "Goodbye!",
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        assert await _redact(app, enabled=True) == {"enabled": True, "entities": None}
        call, ended = await _call(app, AGENT)
        transcript = (await app.api.get(f"/v1/calls/{call.call_id}/transcript")).json()
        events = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()
        record = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]
        webhooks = list(app.sink.events)

    # The model was given what the caller said.
    assert NUMBER in _user_text(given) and EMAIL in _user_text(given)

    # The transcript read: both sides, redacted.
    said = [(e["role"], e["text"]) for e in transcript["data"]["entries"]]
    assert (
        "customer",
        "My number is <PHONE_NUMBER> and my email is <EMAIL_ADDRESS>.",
    ) in said
    assert ("assistant", "Got it, <PHONE_NUMBER>.") in said

    # transcript.final as subscribers got it.
    finals = [e["payload"]["text"] for e in app.sink.of("transcript.final")]
    assert "My number is <PHONE_NUMBER> and my email is <EMAIL_ADDRESS>." in finals
    assert "Got it, <PHONE_NUMBER>." in finals

    # The call.ended webhook: its transcript and the call's numbers.
    payload = ended["payload"]
    assert ended["_signature_valid"] is True
    assert payload["from_number"] == payload["to_number"] == "<PHONE_NUMBER>"
    assert {t["text"] for t in payload["transcript"]} >= {
        "My number is <PHONE_NUMBER> and my email is <EMAIL_ADDRESS>.",
        "Got it, <PHONE_NUMBER>.",
    }

    # Nothing stored or sent carries a raw value...
    assert _raw_in(events) == []
    assert _raw_in(webhooks) == []
    # ...while the call record keeps the number: routing, the suppression list
    # and campaigns read it.
    assert record["from_number"] == CALLER


async def test_post_call_analysis_and_takeaways_read_the_raw_conversation(
    monkeypatch,
) -> None:
    """Extracting a callback number from `<PHONE_NUMBER>` is no extraction:
    they read what was said. What they produce leaves through the exit point
    like the rest of `call.ended`."""
    import turncall.services.call_analysis as call_analysis

    prompts: list[str] = []

    async def analysis_llm(_llm: Any, messages: list[dict[str, str]], **_: Any) -> Any:
        prompts.append(messages[-1]["content"])
        if "Takeaway: callback" in messages[-1]["content"]:
            return SimpleNamespace(text=json.dumps({"number": NUMBER}))
        return SimpleNamespace(text=json.dumps({"summary": f"Call back on {NUMBER}."}))

    monkeypatch.setattr(call_analysis, "complete_text", analysis_llm)
    script = Script(caller=(SAID,), agent=(ToolCall("end_call"), "Goodbye!"))
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redact(app, enabled=True)
        takeaway = await app.api.post(
            "/v1/takeaways",
            json={
                "name": "callback",
                "schema": {
                    "type": "object",
                    "properties": {"number": {"type": "string"}},
                    "required": ["number"],
                },
            },
        )
        assert takeaway.status_code == 201, takeaway.text
        agent = {
            **AGENT,
            "analysis": {
                "enabled": True,
                "takeaway_ids": [takeaway.json()["data"]["id"]],
            },
        }
        _, ended = await _call(app, agent)

    analysis, takeaways = (
        [p for p in prompts if "Takeaway: callback" not in p],
        [p for p in prompts if "Takeaway: callback" in p],
    )
    assert analysis and takeaways
    for prompt in analysis + takeaways:
        assert NUMBER in prompt and EMAIL in prompt, prompt
    payload = ended["payload"]
    assert payload["summary"] == "Call back on <PHONE_NUMBER>."
    assert payload["analysis"]["takeaways"]["callback"]["result"] == {
        "number": "<PHONE_NUMBER>"
    }


async def test_the_warm_transfer_operator_is_briefed_on_what_was_said(
    monkeypatch,
) -> None:
    """The briefing is the call's own business: built from the conversation as
    it was said, not from the stored (redacted) transcript."""
    import turncall.services.llm_text as llm_text
    import turncall.storage.redis as redis_module

    class MemoryRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        async def setex(self, key: str, _ttl: int, value: str) -> None:
            self.values[key] = value

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def delete(self, key: str) -> None:
            self.values.pop(key, None)

    async def summarize(_llm: Any, messages: list[dict[str, str]], **_: Any) -> Any:
        return SimpleNamespace(
            text=messages[-1]["content"].removeprefix("Transcript:\n")
        )

    redis = MemoryRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: redis)
    monkeypatch.setattr(llm_text, "complete_text", summarize)
    script = Script(
        caller=(SAID,),
        agent=(
            ToolCall(
                "transfer_call",
                {
                    "target_number": OPERATOR,
                    "transfer_mode": "warm",
                    "briefing": {"from_summary": True},
                },
            ),
            "Connecting you now.",
        ),
    )
    agent = {
        **AGENT,
        "tools": [
            {
                "name": "transfer_call",
                "description": "Transfer the caller",
                "parameters_schema": {"type": "object", "properties": {}},
            }
        ],
    }
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redact(app, enabled=True)
        number = await app.bind_number(await app.create_agent(agent))
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        heard = await call.complete_transfer(answered_by="human")
        await app.sink.wait_for("call.ended")
        finals = [e["payload"]["text"] for e in app.sink.of("transcript.final")]

    [briefing] = heard.operator
    assert f"Caller: {SAID}" in briefing
    assert "My number is <PHONE_NUMBER> and my email is <EMAIL_ADDRESS>." in finals


async def test_a_project_with_redaction_off_is_untouched(monkeypatch) -> None:
    """As before redaction existed, and the detector is never built."""
    from turncall.services import redaction

    built: list[str] = []

    def build() -> Any:
        built.append("detector")
        raise AssertionError("a project with redaction off loaded the detector")

    # Unloaded, whatever an earlier test in this process did: any use builds.
    monkeypatch.setattr(redaction, "_DETECTOR", None)
    monkeypatch.setattr(redaction, "_build_detector", build)
    script = Script(caller=(SAID,), agent=(ToolCall("end_call"), "Goodbye!"))
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        project = (await app.api.get(f"/v1/projects/{app.project_id}")).json()
        _, ended = await _call(app, AGENT)

    assert project["data"]["redaction"] == {"enabled": False, "entities": None}
    assert built == []
    payload = ended["payload"]
    assert payload["from_number"] == CALLER
    assert ("customer", SAID) in [(t["role"], t["text"]) for t in payload["transcript"]]
    assert SAID in [e["payload"]["text"] for e in app.sink.of("transcript.final")]


async def test_only_an_admin_key_sets_redaction(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        url = f"/v1/projects/{app.project_id}/redaction"
        refused = {}
        for role in ("developer", "viewer"):
            key = await app.api.post(
                "/v1/api-keys/authenticated", json={"name": role, "role": role}
            )
            assert key.status_code == 201, key.text
            response = await app.api.put(
                url,
                json={"enabled": True},
                headers={"Authorization": f"Bearer {key.json()['data']['raw_key']}"},
            )
            refused[role] = response.status_code
        unknown = await app.api.put(url, json={"enabled": True, "entities": ["SSN"]})
        chosen = await _redact(app, enabled=True, entities=["PERSON", "PHONE_NUMBER"])
        read = (await app.api.get(f"/v1/projects/{app.project_id}")).json()
        other = await app.api.put(
            "/v1/projects/00000000-0000-0000-0000-000000000001/redaction",
            json={"enabled": True},
        )

    assert refused == {"developer": 403, "viewer": 403}
    assert unknown.status_code == 422
    assert chosen == {"enabled": True, "entities": ["PERSON", "PHONE_NUMBER"]}
    assert read["data"]["redaction"] == chosen
    assert other.status_code == 403
