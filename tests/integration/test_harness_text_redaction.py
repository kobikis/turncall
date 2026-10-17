"""Redaction of text conversations, end to end (#314, ADR-0024).

SMS, WhatsApp and the Chat API keep a conversation as `sms_messages` rows and
replay them to the model every turn, so while a session is live its rows stay
raw (the agent remembers the number it was given) and everything that reads
them on the way out is redacted: the Chat API's messages and sessions, and a
scenario derived from the session. Once the session ends its rows are
rewritten redacted, as a call's lines are stored.

Driven through Seam A with the real detector; the text LLM is stubbed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from tests.harness import HARNESS_AGENT, post_signed, run_app
from tests.integration.limits_support import SCRIPT
from tests.integration.test_session_message_rate import _sms_number, _whatsapp
from turncall.services import redaction, text_redaction
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration

NUMBER = "415 555 0132"
EMAIL = "jane.doe@example.com"
SAID = f"My number is {NUMBER} and my email is {EMAIL}."
REPLY = f"Got it, {NUMBER}."
CUSTOMER = "+15550004444"
WA_ID = "15559876543"  # what `_whatsapp` sends as the sender

REDACTED_SAID = "My number is <PHONE_NUMBER> and my email is <EMAIL_ADDRESS>."
REDACTED_REPLY = "Got it, <PHONE_NUMBER>."


@pytest.fixture
def turns(monkeypatch) -> list[list[dict]]:
    """The text LLM, stubbed: what each turn was given; it repeats the number."""
    seen: list[list[dict]] = []

    async def _complete(_config, messages, **_kwargs) -> CompletionResult:
        seen.append(messages)
        return CompletionResult(text=REPLY, total_tokens=1)

    monkeypatch.setattr("turncall.services.sms_chat.complete_text", _complete)
    return seen


def _raw_in(document: Any) -> list[str]:
    text = json.dumps(document)
    return [raw for raw in (NUMBER, EMAIL, CUSTOMER, WA_ID) if raw in text]


async def _redact(app: Any, enabled: bool = True) -> None:
    r = await app.api.put(
        f"/v1/projects/{app.project_id}/redaction", json={"enabled": enabled}
    )
    assert r.status_code == 200, r.text
    if enabled:
        assert await redaction.ready()


class _Db:
    """The app's database, from the test: rows at rest, and the sweep."""

    def __init__(self) -> None:
        from turncall.config.settings import Settings
        from turncall.storage.database import create_engine, create_session_factory

        self.engine = create_engine(Settings().database)
        self.sessions = create_session_factory(self.engine)

    async def rows(self, session_id: str) -> tuple[Any, list[Any]]:
        from turncall.storage.models import SmsMessageRow, SmsSessionRow

        async with self.sessions() as db:
            session = await db.get(SmsSessionRow, UUID(session_id))
            messages = (
                await db.scalars(
                    select(SmsMessageRow)
                    .where(SmsMessageRow.session_id == UUID(session_id))
                    .order_by(SmsMessageRow.created_at)
                )
            ).all()
        return session, list(messages)

    async def expire_by_ttl(self, session_id: str) -> None:
        """The session past its TTL, as the sweep finds it, then the sweep's
        rewrite. (The database is shared: counts would be anyone's.)"""
        from turncall.storage.models import SmsSessionRow
        from turncall.storage.repositories import sms_session_repo

        async with self.sessions() as db:
            await db.execute(
                update(SmsSessionRow)
                .where(SmsSessionRow.id == UUID(session_id))
                .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            await sms_session_repo.expire_stale_sessions(db)
            await db.commit()
        await text_redaction.seal_ended_sessions(self.sessions)

    async def close(self) -> None:
        await self.engine.dispose()


@pytest.fixture
async def db():
    handle = _Db()
    yield handle
    await handle.close()


async def test_a_chat_is_redacted_on_the_way_out_and_once_it_ends(
    monkeypatch, turns, db
) -> None:
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redact(app)
        agent_id = await app.create_agent(HARNESS_AGENT)
        first = await app.api.post(
            "/v1/chat",
            json={"agent_id": agent_id, "message": SAID, "customer_number": CUSTOMER},
        )
        assert first.status_code == 201, first.text
        session_id = first.json()["data"]["session_id"]
        second = await app.api.post(
            "/v1/chat",
            json={"agent_id": agent_id, "session_id": session_id, "message": "Ok?"},
        )
        live = {
            "messages": (
                await app.api.get(f"/v1/chat/sessions/{session_id}/messages")
            ).json(),
            "sessions": (await app.api.get("/v1/chat/sessions")).json(),
            "session": (await app.api.get(f"/v1/chat/sessions/{session_id}")).json(),
            "scenario": (
                await app.api.post(
                    "/v1/eval-scenarios/from-session", json={"session_id": session_id}
                )
            ).json(),
        }
        stored_live, rows_live = await db.rows(session_id)

        assert (await app.api.delete(f"/v1/chat/sessions/{session_id}")).is_success
        await text_redaction.seal_ended_sessions(db.sessions)
        ended = (await app.api.get(f"/v1/chat/sessions/{session_id}/messages")).json()
        ended_session = (await app.api.get(f"/v1/chat/sessions/{session_id}")).json()
        stored, rows = await db.rows(session_id)

    # The API caller's own reply is the live conversation: raw.
    assert second.json()["data"]["reply"] == REPLY
    # The model's second turn replayed the first, raw: it kept the number.
    replayed = " ".join(m["content"] for m in turns[1])
    assert SAID in replayed and REPLY in replayed

    # While live: raw at rest (the model reads it), redacted on every read.
    assert [r.content for r in rows_live] == [SAID, REPLY, "Ok?", REPLY]
    assert stored_live.customer_number == CUSTOMER
    contents = [m["content"] for m in live["messages"]["data"]]
    assert contents == [REDACTED_SAID, REDACTED_REPLY, "Ok?", REDACTED_REPLY]
    assert live["session"]["data"]["customer_number"] == "<PHONE_NUMBER>"
    assert live["session"]["data"]["turncall_number"] == "api"
    assert _raw_in(live) == []

    # Ended: rewritten at rest, and read back the same.
    assert [r.content for r in rows] == [
        REDACTED_SAID,
        REDACTED_REPLY,
        "Ok?",
        REDACTED_REPLY,
    ]
    assert stored.customer_number == "<PHONE_NUMBER>"
    assert stored.metadata_json["redaction"] == "done"
    assert [m["content"] for m in ended["data"]] == contents
    assert ended_session["data"]["customer_number"] == "<PHONE_NUMBER>"


@pytest.mark.parametrize("channel", ["sms", "whatsapp"])
async def test_sms_and_whatsapp_are_redacted_on_the_way_out_and_once_they_expire(
    monkeypatch, turns, db, channel
) -> None:
    sent: list[str] = []

    async def _send(_http, *, text: str, **_kwargs) -> dict:
        sent.append(text)
        return {}

    monkeypatch.setattr("turncall.services.whatsapp_chat.send_whatsapp_text", _send)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redact(app)
        agent_id = await app.create_agent(HARNESS_AGENT)
        number = await _sms_number(app, agent_id, whatsapp=channel == "whatsapp")
        for sid, text in (("SM1", SAID), ("SM2", "Ok?")):
            if channel == "sms":
                r = await post_signed(
                    app.api,
                    app.carrier,
                    app.carrier.number(number).sms_url,
                    {
                        "AccountSid": app.carrier.account_sid,
                        "MessageSid": sid,
                        "From": CUSTOMER,
                        "To": number,
                        "Body": text,
                    },
                )
            else:
                body = _whatsapp(number, text)
                [message] = body["entry"][0]["changes"][0]["value"]["messages"]
                message["id"] = f"wamid.{sid}"  # not the text: a 64-char column
                r = await app.api.post("/webhooks/whatsapp", json=body)
            assert r.status_code == 200, r.text
        [session] = (await app.api.get("/v1/chat/sessions")).json()["data"]
        messages = (
            await app.api.get(f"/v1/chat/sessions/{session['id']}/messages")
        ).json()
        _, rows_live = await db.rows(session["id"])

        await db.expire_by_ttl(session["id"])
        stored, rows = await db.rows(session["id"])
        ended = (await app.api.get(f"/v1/chat/sessions/{session['id']}")).json()

    # The customer was answered with the raw reply, and the model kept the number.
    if channel == "sms":
        assert REPLY in r.text
    else:
        assert sent == [REPLY, REPLY]
    assert SAID in " ".join(m["content"] for m in turns[1])

    assert [row.content for row in rows_live] == [SAID, REPLY, "Ok?", REPLY]
    assert session["customer_number"] == session["turncall_number"] == "<PHONE_NUMBER>"
    assert [m["content"] for m in messages["data"]] == [
        REDACTED_SAID,
        REDACTED_REPLY,
        "Ok?",
        REDACTED_REPLY,
    ]
    assert _raw_in([session, messages]) == []

    assert [row.content for row in rows] == [
        REDACTED_SAID,
        REDACTED_REPLY,
        "Ok?",
        REDACTED_REPLY,
    ]
    assert stored.customer_number == stored.turncall_number == "<PHONE_NUMBER>"
    assert ended["data"]["status"] == "expired"
    assert _raw_in(ended) == []


async def test_a_session_that_ended_before_redaction_was_on_is_not_rewritten(
    monkeypatch, turns, db
) -> None:
    """As a call's stored lines are not; while the project redacts, its reads
    are redacted all the same."""
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        r = await app.api.post("/v1/chat", json={"agent_id": agent_id, "message": SAID})
        session_id = r.json()["data"]["session_id"]
        off = (await app.api.get(f"/v1/chat/sessions/{session_id}/messages")).json()
        assert (await app.api.delete(f"/v1/chat/sessions/{session_id}")).is_success

        await _redact(app)
        await text_redaction.seal_ended_sessions(db.sessions)
        on = (await app.api.get(f"/v1/chat/sessions/{session_id}/messages")).json()
        stored, rows = await db.rows(session_id)

    assert [m["content"] for m in off["data"]] == [SAID, REPLY]
    assert "redaction" not in stored.metadata_json
    assert [row.content for row in rows] == [SAID, REPLY]
    assert [m["content"] for m in on["data"]] == [REDACTED_SAID, REDACTED_REPLY]


async def test_a_rewrite_the_detector_cannot_answer_waits_for_the_next_sweep(
    monkeypatch, turns, db
) -> None:
    """Never `<REDACTED>` stored for good because the detector was down."""
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redact(app)
        agent_id = await app.create_agent(HARNESS_AGENT)
        r = await app.api.post("/v1/chat", json={"agent_id": agent_id, "message": SAID})
        session_id = r.json()["data"]["session_id"]
        assert (await app.api.delete(f"/v1/chat/sessions/{session_id}")).is_success

        async def unanswered(_texts, _entities) -> None:
            return None

        with monkeypatch.context() as down:
            down.setattr(redaction.ENGINE, "redact", unanswered)
            await text_redaction.seal_ended_sessions(db.sessions)
            deferred, deferred_rows = await db.rows(session_id)
        await text_redaction.seal_ended_sessions(db.sessions)
        stored, rows = await db.rows(session_id)

    assert deferred.metadata_json["redaction"] == "pending"
    assert [row.content for row in deferred_rows] == [SAID, REPLY]
    assert stored.metadata_json["redaction"] == "done"
    assert [row.content for row in rows] == [REDACTED_SAID, REDACTED_REPLY]
