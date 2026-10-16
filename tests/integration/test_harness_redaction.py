"""Redaction, end to end (#313, ADR-0024).

A project that turns redaction on gets its callers' numbers and emails
replaced in everything it stores and sends — the transcript read, the call's
event log, every webhook — while the agent, during the call, works with the
raw values: the model is given them, the operator of a warm transfer is
briefed with them, and post-call analysis and takeaways read them. A project
with redaction off is untouched and never loads the detector.

Driven through Seam A: the real app, stub providers, a fake carrier, a signed
webhook subscriber. The detector is the real one, Presidio over spaCy's small
model in its worker process. It is loaded before each call is placed
(`_redact`), as `PUT /redaction` starts it in the background: no request or
carrier webhook may be what waits for the model's load.
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
from tests.harness.second_instance import second_instance
from tests.harness.twilio import post_signed
from turncall.services import redaction

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
    """Set the project's redaction; turned on, wait for the detector the PUT
    started loading, so the call that follows is not what pays for it."""
    response = await app.api.put(
        f"/v1/projects/{app.project_id}/redaction", json=setting
    )
    assert response.status_code == 200, response.text
    if setting.get("enabled"):
        assert await redaction.ready()
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
            return SimpleNamespace(
                text=json.dumps({"number": NUMBER, "customer_id": EMAIL})
            )
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
                    "properties": {
                        "number": {"type": "string"},
                        "customer_id": {"type": "string"},
                    },
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
        call, ended = await _call(app, agent)
        record = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]
        stored = (await app.api.get(f"/v1/calls/{call.call_id}/analysis")).json()

    analysis, takeaways = (
        [p for p in prompts if "Takeaway: callback" not in p],
        [p for p in prompts if "Takeaway: callback" in p],
    )
    assert analysis and takeaways
    for prompt in analysis + takeaways:
        assert NUMBER in prompt and EMAIL in prompt, prompt
    # A takeaway's fields are the user's: one named `customer_id` holds what
    # the caller said, and is analysed like the rest (#314).
    extracted = {"number": "<PHONE_NUMBER>", "customer_id": "<EMAIL_ADDRESS>"}
    payload = ended["payload"]
    assert payload["summary"] == "Call back on <PHONE_NUMBER>."
    assert payload["analysis"]["takeaways"]["callback"]["result"] == extracted
    # Stored redacted on the call, so the call read serves no more than the
    # transcript does (#327 review, 4).
    assert record["analysis"]["summary"] == "Call back on <PHONE_NUMBER>."
    assert record["analysis"]["takeaways"]["callback"]["result"] == extracted
    assert _raw_in(record["analysis"]) == []
    assert _raw_in(stored) == []


async def test_tool_invocations_are_stored_redacted_but_the_tool_got_them_raw(
    monkeypatch,
) -> None:
    """The tool's webhook is part of the live call: it is sent what the model
    passed, the caller's number and email included, and the model is handed
    its answer as it was. The invocation record outlives the call, so it is
    stored redacted, whatever the tool named its fields (#314)."""
    import asyncio

    from aiohttp import web

    arguments = {"callback": NUMBER, "customer_id": EMAIL}
    answer = {"booked": True, "confirmation": f"We will email {EMAIL}."}
    received: list[dict[str, Any]] = []

    async def book(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response(answer)

    tool_app = web.Application()
    tool_app.router.add_post("/book", book)
    runner = web.AppRunner(tool_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    given: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            ToolCall("book_callback", arguments),
            _remembering(given, ("Booked.", ToolCall("end_call"))),
            "Goodbye!",
        ),
    )
    agent = {
        **AGENT,
        "tools": [
            END_CALL,
            {
                "name": "book_callback",
                "description": "Book a callback",
                "parameters_schema": {"type": "object", "properties": {}},
                "webhook_url": f"http://127.0.0.1:{port}/book",
            },
        ],
    }
    try:
        async with run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app:
            await _redact(app, enabled=True)
            call, _ = await _call(app, agent)
            # Recorded off the tool's critical path: wait for both rows.
            for _ in range(100):
                read = await app.api.get(f"/v1/tools/invocations/{call.call_id}")
                rows = read.json()["data"]
                if len(rows) >= 2:
                    break
                await asyncio.sleep(0.1)
    finally:
        await runner.cleanup()

    # The live call: the webhook got the arguments, the model the answer.
    assert [r["arguments"] for r in received] == [arguments]
    tool_said = " ".join(
        str(m.get("content"))
        for context in given
        for m in context
        if m["role"] == "tool"
    )
    assert EMAIL in tool_said

    # The record: redacted, `customer_id` included.
    [booked] = [r for r in rows if r["tool_name"] == "book_callback"]
    assert booked["status"] == "succeeded"
    assert booked["input_json"] == {
        "callback": "<PHONE_NUMBER>",
        "customer_id": "<EMAIL_ADDRESS>",
    }
    assert booked["output_json"] == {
        "booked": True,
        "confirmation": "We will email <EMAIL_ADDRESS>.",
    }
    assert _raw_in(rows) == []


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
    """As before redaction existed, and the detector is never built: its
    worker process is never started."""
    built: list[str] = []

    def start() -> Any:
        built.append("detector")
        raise AssertionError("a project with redaction off loaded the detector")

    # Unloaded, whatever an earlier test in this process did: any use starts it.
    monkeypatch.setattr(redaction, "ENGINE", redaction._Engine())
    monkeypatch.setattr(redaction, "_new_pool", start)
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
        # `enabled` is required: a PUT naming only entities used to turn it off.
        partial = await app.api.put(url, json={"entities": ["PERSON"]})
        chosen = await _redact(app, enabled=True, entities=["PERSON", "PHONE_NUMBER"])
        read = (await app.api.get(f"/v1/projects/{app.project_id}")).json()
        other = await app.api.put(
            "/v1/projects/00000000-0000-0000-0000-000000000001/redaction",
            json={"enabled": True},
        )

    assert refused == {"developer": 403, "viewer": 403}
    assert unknown.status_code == 422
    assert partial.status_code == 422
    assert chosen == {"enabled": True, "entities": ["PERSON", "PHONE_NUMBER"]}
    assert read["data"]["redaction"] == chosen
    assert other.status_code == 403


async def test_a_whisper_on_another_replica_is_briefed_by_the_calls_process(
    monkeypatch,
) -> None:
    """The conversation lives only in the process that ran the call. When
    Twilio fetches the warm transfer's whisper from another replica, that
    replica asks the call's process for the briefing (Redis pub/sub, carrying
    the call's id and nothing else) instead of briefing from the redacted
    transcript (#327 review, 6). The other replica's LLM is a real one with no
    key: a briefing it made itself would be the fallback line."""
    import turncall.services.llm_text as llm_text

    async def summarize(_llm: Any, messages: list[dict[str, str]], **_: Any) -> Any:
        return SimpleNamespace(
            text=messages[-1]["content"].removeprefix("Transcript:\n")
        )

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
    async with (
        run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        second_instance() as other,
    ):
        await _redact(app, enabled=True)
        number = await app.bind_number(await app.create_agent(agent))
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)

        # The operator answers; Twilio fetches the whisper from the other server.
        transfer = await app.carrier.wait_for("transfer", timeout=10)
        await call.close_stream()
        whisper_path = transfer.params["whisper_url"].removeprefix(app.base_url)
        whisper = await post_signed(
            other,
            app.carrier,
            f"{other.base_url}{whisper_path}",
            {
                "AccountSid": app.carrier.account_sid,
                "CallSid": "CA" + "0" * 32,
                "From": number,
                "To": OPERATOR,
            },
        )
        await call.finish()
        await app.sink.wait_for("call.ended")

    assert whisper.status_code == 200, whisper.text
    assert f"Caller: {SAID}" in whisper.text


async def test_post_call_triggered_on_another_replica_runs_in_the_calls_process(
    monkeypatch,
) -> None:
    """The call is ended through the API on another replica, which is where
    post-call processing is triggered. It hands the run to the call's process,
    so analysis reads what was said (#327 review, 6). The other replica's
    analysis LLM is a real one with no key: an analysis it ran itself would
    carry no summary."""
    import turncall.services.call_analysis as call_analysis

    analysed: list[str] = []

    async def analysis_llm(_llm: Any, messages: list[dict[str, str]], **_: Any) -> Any:
        analysed.append(messages[-1]["content"])
        return SimpleNamespace(text=json.dumps({"summary": f"Call back on {NUMBER}."}))

    monkeypatch.setattr(call_analysis, "complete_text", analysis_llm)
    script = Script(caller=(SAID,), agent=("Thanks, one moment.",))
    agent = {**AGENT, "tools": [], "analysis": {"enabled": True}}
    async with (
        run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        second_instance() as other,
    ):
        other.headers["Authorization"] = app.api.headers["Authorization"]
        await _redact(app, enabled=True)
        number = await app.bind_number(await app.create_agent(agent))
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        ended_there = await other.post(f"/v1/calls/{call.call_id}/end", json={})
        await call.close_stream()
        await call.finish()
        ended = await _all_call_ended(app)

    assert ended_there.status_code == 200, ended_there.text
    assert analysed, "the call's process never analysed the call"
    for prompt in analysed:
        assert NUMBER in prompt and EMAIL in prompt, prompt
    assert ended
    for event in ended:
        assert event["payload"]["summary"] == "Call back on <PHONE_NUMBER>."
        assert _raw_in(event) == []


async def _all_call_ended(app: Harness, settle: float = 3.0) -> list[dict[str, Any]]:
    """Every `call.ended`, once they stop coming (#328 sends some twice)."""
    import asyncio

    await app.sink.wait_for("call.ended")
    seen = -1
    while seen != len(app.sink.of("call.ended")):
        seen = len(app.sink.of("call.ended"))
        await asyncio.sleep(settle)
    return list(app.sink.of("call.ended"))
