"""A forged Twilio webhook on a deployment with no Twilio auth token (#337).

Through the running app: a number bound with voice and SMS, then the platform
token blanked, as a template that leaves `TWILIO_AUTH_TOKEN` empty would. In
production an unsigned voice or SMS webhook is a 403 and leaves nothing behind
— no call, no text session, no LLM turn, no event. In development the same
requests are taken, as they always were.
"""

import secrets

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app
from tests.integration.limits_support import ring
from turncall.config.settings import Environment, get_settings
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration


@pytest.fixture
def llm_turns(monkeypatch) -> list[list[dict]]:
    turns: list[list[dict]] = []

    async def _complete(_config, messages, **_kwargs) -> CompletionResult:
        turns.append(messages)
        return CompletionResult(text="stub reply", total_tokens=1)

    monkeypatch.setattr("turncall.services.sms_chat.complete_text", _complete)
    return turns


def _sms(carrier, to_number: str) -> dict[str, str]:
    return {
        "AccountSid": carrier.account_sid,
        "MessageSid": "SM" + secrets.token_hex(16),
        "From": "+15550004444",
        "To": to_number,
        "Body": "hello",
    }


async def _sms_number(app, agent_id: str) -> str:
    e164 = "+1555" + str(secrets.randbelow(10**7)).zfill(7)
    number = app.carrier.add_number(e164)
    r = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "agent",
            "routing_target_id": agent_id,
            "sms_enabled": True,
        },
    )
    assert r.status_code == 201, r.text
    return e164


async def test_production_refuses_and_development_accepts(
    monkeypatch, redis_env, llm_turns
):
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await _sms_number(app, await app.create_agent(HARNESS_AGENT))
        voice_url = app.carrier.number(number).voice_url
        sms_url = app.carrier.number(number).sms_url
        # The token left blank on a deployment already serving its numbers.
        settings = get_settings()
        monkeypatch.setattr(settings.twilio, "auth_token", "")

        monkeypatch.setattr(settings, "environment", Environment.PRODUCTION)
        refused_voice = await app.api.post(voice_url, data=ring(app.carrier, number))
        refused_sms = await app.api.post(sms_url, data=_sms(app.carrier, number))
        calls_after_refusal = (await app.api.get("/v1/calls")).json()["data"]
        sessions_after_refusal = (await app.api.get("/v1/chat/sessions")).json()
        events_after_refusal = list(app.sink.events)

        monkeypatch.setattr(settings, "environment", Environment.DEVELOPMENT)
        taken_voice = await app.api.post(voice_url, data=ring(app.carrier, number))
        taken_sms = await app.api.post(sms_url, data=_sms(app.carrier, number))
        calls = (await app.api.get("/v1/calls")).json()["data"]
        sessions = (await app.api.get("/v1/chat/sessions")).json()["data"]

    assert refused_voice.status_code == 403
    assert refused_sms.status_code == 403
    # Nothing was created: no call, no text session, no LLM turn, no event.
    assert calls_after_refusal == []
    assert sessions_after_refusal["data"] == []
    assert events_after_refusal == []

    assert taken_voice.status_code == 200
    assert "<Stream" in taken_voice.text
    assert len(calls) == 1
    assert taken_sms.text == "<Response><Message>stub reply</Message></Response>"
    assert len(sessions) == 1
    assert len(llm_turns) == 1
