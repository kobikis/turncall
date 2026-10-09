"""Messages per minute, per text session (#226).

A looping integration or an abusive sender can run a conversation's LLM bill
up as fast as it can post. Past the session's cap the Chat API answers 429,
and an SMS or WhatsApp message is neither answered nor sent to the LLM — the
count shared across servers like every limit.
"""

import pytest

from tests.harness import post_signed, run_app
from tests.harness.second_instance import STUB_TEXT_REPLY, second_instance
from tests.integration.limits_support import SCRIPT, limited_project
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration


@pytest.fixture
def llm_turns(monkeypatch) -> list[list[dict]]:
    """The text LLM, stubbed: records what each turn was asked."""
    turns: list[list[dict]] = []

    async def _complete(_config, messages, **_kwargs) -> CompletionResult:
        turns.append(messages)
        return CompletionResult(text=STUB_TEXT_REPLY, total_tokens=1)

    monkeypatch.setattr("turncall.services.sms_chat.complete_text", _complete)
    return turns


async def test_chat_over_the_cap_is_429_across_servers(
    monkeypatch, redis_env, llm_turns
):
    async with (
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        second_instance() as other,
    ):
        other.headers["Authorization"] = app.api.headers["Authorization"]
        agent_id, _, _ = await limited_project(app, messages_per_minute=2)

        first = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "one"}
        )
        session_id = first.json()["data"]["session_id"]
        turn = {"agent_id": agent_id, "session_id": session_id}
        second = await other.post("/v1/chat", json={**turn, "message": "two"})
        third = await app.api.post("/v1/chat", json={**turn, "message": "three"})
        elsewhere = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "a new session"}
        )
        stored = (await app.api.get(f"/v1/chat/sessions/{session_id}/messages")).json()

    assert first.status_code == second.status_code == 201
    assert third.status_code == 429
    assert third.json()["code"] == "rate_limited"
    details = third.json()["details"]
    assert (details["limit"], details["max"]) == ("messages_per_minute", 2)
    assert 0 < details["retry_after_ms"] <= 60_000
    # Per session: another conversation is not held back by this one.
    assert elsewhere.status_code == 201
    # The refused message was neither stored nor sent to the LLM.
    assert [m["content"] for m in stored["data"] if m["role"] == "customer"] == [
        "one",
        "two",
    ]
    assert len(llm_turns) == 2  # "one" and "a new session"; "two" ran elsewhere


async def test_sms_over_the_cap_gets_no_reply_and_no_llm(
    monkeypatch, redis_env, llm_turns
):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, _, _ = await limited_project(app, messages_per_minute=1)
        number = await _sms_number(app, agent_id)
        url = app.carrier.number(number).sms_url

        def sms(sid: str, body: str) -> dict[str, str]:
            return {
                "AccountSid": app.carrier.account_sid,
                "MessageSid": sid,
                "From": "+15550004444",
                "To": number,
                "Body": body,
            }

        answered = await post_signed(app.api, app.carrier, url, sms("SM1", "hi"))
        ignored = await post_signed(app.api, app.carrier, url, sms("SM2", "again"))

    assert answered.text == f"<Response><Message>{STUB_TEXT_REPLY}</Message></Response>"
    assert ignored.status_code == 200
    assert ignored.text == "<Response/>"
    assert len(llm_turns) == 1


async def test_whatsapp_over_the_cap_is_not_answered(monkeypatch, redis_env, llm_turns):
    sent: list[str] = []

    async def _send(_http, *, text: str, **_kwargs) -> dict:
        sent.append(text)
        return {}

    monkeypatch.setattr("turncall.services.whatsapp_chat.send_whatsapp_text", _send)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, _, _ = await limited_project(app, messages_per_minute=1)
        number = await _sms_number(app, agent_id, whatsapp=True)
        for text in ("hi", "again"):
            r = await app.api.post("/webhooks/whatsapp", json=_whatsapp(number, text))
            assert r.status_code == 200

    assert sent == [STUB_TEXT_REPLY]
    assert len(llm_turns) == 1


async def test_unset_takes_every_message(monkeypatch, redis_env, llm_turns):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, _, _ = await limited_project(app)
        first = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "0"}
        )
        turn = {"agent_id": agent_id, "session_id": first.json()["data"]["session_id"]}
        rest = [
            await app.api.post("/v1/chat", json={**turn, "message": str(n)})
            for n in range(1, 6)
        ]

    assert [r.status_code for r in [first, *rest]] == [201] * 6


async def _sms_number(app, agent_id: str, *, whatsapp: bool = False) -> str:
    """A number bound with SMS (or WhatsApp) on, so texts reach the agent."""
    import uuid

    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    number = app.carrier.add_number(e164)
    r = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "agent",
            "routing_target_id": agent_id,
            "sms_enabled": not whatsapp,
        },
    )
    assert r.status_code == 201, r.text
    if whatsapp:
        await _enable_whatsapp(e164)
    return e164


async def _enable_whatsapp(e164: str) -> None:
    """The binding API has no `whatsapp_enabled` yet; the column does."""
    from sqlalchemy import update

    from turncall.config.settings import Settings
    from turncall.storage.database import create_engine, create_session_factory
    from turncall.storage.models import PhoneNumberRow

    engine = create_engine(Settings().database)
    async with create_session_factory(engine)() as session:
        await session.execute(
            update(PhoneNumberRow)
            .where(PhoneNumberRow.e164_number == e164)
            .values(whatsapp_enabled=True)
        )
        await session.commit()
    await engine.dispose()


def _whatsapp(number: str, text: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "BIZ",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": number.lstrip("+"),
                                "phone_number_id": "PHONE_ID",
                            },
                            "messages": [
                                {
                                    "from": "15559876543",
                                    "id": f"wamid.{text}",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
