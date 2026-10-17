"""Redaction in logs, end to end (#314, ADR-0024).

A project with redaction on has its callers' numbers and emails redacted in
every log line its calls and text conversations write: the carrier webhook's,
the call's pipeline's (pipecat's observers log the transcript), the SMS and
WhatsApp handlers'. A project with redaction off logs them as before.

Driven through Seam A, with a loguru sink like production's (`enqueue=True`,
JSON): the patcher runs on the logging thread, before the record is queued.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from loguru import logger

from tests.harness import (
    HARNESS_AGENT,
    Harness,
    Script,
    ToolCall,
    place_inbound_call,
    post_signed,
    run_app,
)
from tests.integration.test_session_message_rate import _sms_number, _whatsapp
from turncall.services import redaction
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
NUMBER = "415 555 0132"
EMAIL = "jane.doe@example.com"
WHATSAPP_CALLER = "15559876543"  # `_whatsapp`'s sender, a wa_id
SAID = f"My number is {NUMBER} and my email is {EMAIL}."

END_CALL = {
    "name": "end_call",
    "description": "End the call",
    "parameters_schema": {"type": "object", "properties": {}},
}
AGENT = {**HARNESS_AGENT, "first_message": "Hello.", "tools": [END_CALL]}


async def _call(app: Harness) -> None:
    """The caller rings, says SAID, and the agent hangs up."""
    number = await app.bind_number(await app.create_agent(AGENT))
    call = await place_inbound_call(
        app.carrier, app.api, from_number=CALLER, to_number=number
    )
    await call.wait_for_agent_speech(1)  # the greeting
    await call.say(400)
    await call.closed.wait()
    await call.finish()
    await app.sink.wait_for("call.ended")


async def _text(app: Harness) -> None:
    """The caller texts SAID, by SMS and on WhatsApp."""
    agent_id = await app.create_agent(AGENT)
    number = await _sms_number(app, agent_id)
    sms = {
        "AccountSid": app.carrier.account_sid,
        "MessageSid": "SM1",
        "From": CALLER,
        "To": number,
        "Body": SAID,
    }
    url = app.carrier.number(number).sms_url
    assert (await post_signed(app.api, app.carrier, url, sms)).status_code == 200
    number = await _sms_number(app, agent_id, whatsapp=True)
    payload = _whatsapp(number, SAID)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.1"
    message = await app.api.post("/webhooks/whatsapp", json=payload)
    assert message.status_code == 200


async def _unsupported(app: Harness) -> None:
    """The caller sends a WhatsApp image: not answered, but logged."""
    number = await _sms_number(app, await app.create_agent(AGENT), whatsapp=True)
    payload = _whatsapp(number, SAID)
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.update(type="image", id="wamid.image")
    del message["text"]
    response = await app.api.post("/webhooks/whatsapp", json=payload)
    assert response.status_code == 200


async def _logged_during(
    monkeypatch, drive: Callable[[Harness], Awaitable[None]], *, enabled: bool
) -> list[dict[str, Any]]:
    """Every record logged while `drive` runs, as the JSON sink wrote it."""
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", serialize=True, enqueue=True)
    script = Script(
        caller=(SAID,),
        agent=((f"Got it, {NUMBER}.", ToolCall("end_call")), "Goodbye!"),
    )
    try:
        async with run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app:
            if enabled:
                response = await app.api.put(
                    f"/v1/projects/{app.project_id}/redaction", json={"enabled": True}
                )
                assert response.status_code == 200, response.text
                assert await redaction.ready()
            await drive(app)
        await logger.complete()
    finally:
        logger.remove(sink)
    return [json.loads(line)["record"] for line in lines]


def _with(records: list[dict[str, Any]], raw: str) -> list[str]:
    """The records still carrying `raw`, by message."""
    return [
        r["message"]
        for r in records
        if raw in r["message"] or raw in json.dumps(r["extra"])
    ]


def _extra(records: list[dict[str, Any]], message: str, key: str) -> list[Any]:
    return [r["extra"][key] for r in records if r["message"] == message]


async def test_a_redacting_projects_calls_log_no_caller_numbers_or_emails(
    monkeypatch,
) -> None:
    records = await _logged_during(monkeypatch, _call, enabled=True)

    assert _with(records, CALLER) == []
    assert _with(records, NUMBER) == []
    assert _with(records, EMAIL) == []
    # The lines are still there, redacted: the carrier webhook's, and the
    # pipeline's (pipecat's observers log what was said).
    assert _extra(records, "twilio_inbound_call", "from_number") == ["<PHONE_NUMBER>"]
    pipeline = [r["message"] for r in records if r["name"].startswith("pipecat.")]
    assert any(
        "My number is <PHONE_NUMBER> and my email is <EMAIL_ADDRESS>." in m
        for m in pipeline
    )
    # Ids stay as they were.
    assert all("<" not in r["extra"].get("call_id", "") for r in records)


async def test_a_project_with_redaction_off_logs_as_before(monkeypatch) -> None:
    records = await _logged_during(monkeypatch, _call, enabled=False)

    assert _extra(records, "twilio_inbound_call", "from_number") == [CALLER]
    assert any(SAID in r["message"] for r in records)


async def test_a_redacting_projects_texts_log_no_caller_numbers(
    monkeypatch, llm_replies
) -> None:
    records = await _logged_during(monkeypatch, _text, enabled=True)

    assert llm_replies, "no text reached the agent"
    assert _with(records, CALLER) == []
    assert _with(records, WHATSAPP_CALLER) == []
    assert _extra(records, "twilio_inbound_sms", "from_number") == ["<PHONE_NUMBER>"]
    assert _extra(records, "whatsapp_inbound_text", "from_number") == ["<PHONE_NUMBER>"]


async def test_an_unanswered_whatsapp_message_logs_no_caller_number(
    monkeypatch, llm_replies
) -> None:
    """Its line is written before any reply is tried: the project is known
    from the business number first."""
    records = await _logged_during(monkeypatch, _unsupported, enabled=True)

    assert _with(records, WHATSAPP_CALLER) == []
    assert _extra(records, "whatsapp_unsupported_message_type", "from_number") == [
        "<PHONE_NUMBER>"
    ]


async def test_the_policy_is_read_on_the_handlers_open_session(
    monkeypatch, llm_replies
) -> None:
    """An API request and a carrier webhook already hold a session (the key's
    project, the number's row were just read on it): the policy is read
    there on a cache miss, not on a second pooled connection."""
    alone: list[object] = []
    read_alone = redaction._read_policy_alone

    async def recording(project_id, sessions=None):
        alone.append(project_id)
        return await read_alone(project_id, sessions)

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        response = await app.api.put(
            f"/v1/projects/{app.project_id}/redaction", json={"enabled": True}
        )
        assert response.status_code == 200, response.text
        number = await _sms_number(app, await app.create_agent(AGENT))
        whatsapp = await _sms_number(app, await app.create_agent(AGENT), whatsapp=True)
        monkeypatch.setattr(redaction, "_read_policy_alone", recording)

        redaction.forget_policies()
        assert (await app.api.get("/v1/chat/sessions")).status_code == 200
        redaction.forget_policies()
        sms = {
            "AccountSid": app.carrier.account_sid,
            "MessageSid": "SM1",
            "From": CALLER,
            "To": number,
            "Body": "hello",
        }
        url = app.carrier.number(number).sms_url
        assert (await post_signed(app.api, app.carrier, url, sms)).status_code == 200
        redaction.forget_policies()
        payload = _whatsapp(whatsapp, "hello")
        response = await app.api.post("/webhooks/whatsapp", json=payload)
        assert response.status_code == 200

    assert alone == []


@pytest.fixture
def llm_replies(monkeypatch) -> list[list[dict]]:
    """The text LLM, stubbed: records what each turn was asked."""
    turns: list[list[dict]] = []

    async def _complete(_config, messages, **_kwargs) -> CompletionResult:
        turns.append(messages)
        return CompletionResult(text="Thanks.", total_tokens=1)

    async def _send(_http, **_kwargs) -> dict:
        return {}

    monkeypatch.setattr("turncall.services.sms_chat.complete_text", _complete)
    monkeypatch.setattr("turncall.services.whatsapp_chat.send_whatsapp_text", _send)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    return turns
