"""The build takes an accepted agent; its copies of the rules are gone (#481).

Over the running app, stub providers, a fake carrier.

The rules the build used to hold a second copy of — xAI's LLM and TTS rules,
Grok Voice's, the Flux mixing rule, Eleven Realtime's — now refuse a call-init
inline agent at call-init, before the caller is answered, in the very words
the rule functions write (the procedures graph rule's harness cases are
`test_harness_call_procedure_rules.py`).

And every path to the build hands it an accepted agent, so a stored agent
saved before a newer rule existed is refused wherever it is run: a WebRTC
connect naming it (a 422 with the reason), a WhatsApp number bound to it
(rejected unanswered), and an outbound dial's own agent — which nothing
accepted at the dial — when its media stream opens, failing the call with the
reason as a build that raised once did.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterator

import pytest
from loguru import logger

from tests.harness import HARNESS_AGENT, Script, answer_outbound_call, run_app
from tests.integration.test_call_init_project_scope import (
    _calls,
    _ring_refused,
    _route,
    no_peer,  # noqa: F401 - a fixture
)
from tests.integration.test_call_init_project_scope_whatsapp import (
    _connect_whatsapp,
    _live_calls,
    assert_whatsapp_rejected,
    whatsapp_voice,  # noqa: F401 - a fixture
)
from tests.integration.test_handoff_stored_acceptance import _stored_before_the_rule
from tests.integration.test_session_message_rate import _enable_whatsapp
from tests.integration.test_text_session_variables import CallInitServer
from turncall.domain.models import LLMConfig, S2SConfig, TTSConfig
from turncall.orchestrator import eleven_realtime, grok_voice, xai_config
from turncall.orchestrator.deepgram_flux import MIXED_FLUX_GROUP

pytestmark = pytest.mark.integration

XAI_REASON = "base_url is not supported for the xai provider"
_ELEVEN = {"pipeline_mode": "s2s", "s2s": {"provider": "elevenlabs"}}

# (the inline agent's change, the field refused, the reason as the rule writes it)
FORMER_BUILD_RULES = {
    "xai_llm_minimal": (
        {"llm": {"provider": "xai", "reasoning_effort": "minimal"}},
        "llm",
        xai_config.llm_refusal(LLMConfig(provider="xai", reasoning_effort="minimal")),
    ),
    "xai_llm_failover_base_url": (
        {"llm": {"failover": [{"provider": "xai", "base_url": "https://gw.example"}]}},
        "llm.failover[0]",
        xai_config.llm_refusal(LLMConfig(provider="xai", base_url="https://gw.x")),
    ),
    "xai_tts_speed": (
        {"tts": {"provider": "xai", "speed": 2.0}},
        "tts",
        xai_config.tts_refusal(TTSConfig(provider="xai", speed=2.0)),
    ),
    "xai_tts_failover_speed": (
        {"tts": {"failover": [{"provider": "xai", "speed": 0.5}]}},
        "tts.failover[0]",
        xai_config.tts_refusal(TTSConfig(provider="xai", speed=0.5)),
    ),
    "grok_voice_temperature": (
        {"pipeline_mode": "s2s", "s2s": {"provider": "xai", "temperature": 0.5}},
        "s2s",
        grok_voice.refusal(S2SConfig(provider="xai", temperature=0.5)),
    ),
    "grok_voice_extra": (
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "xai", "extra": {"reasoning": {"effort": "low"}}},
        },
        "s2s",
        grok_voice.refusal(
            S2SConfig(provider="xai", extra={"reasoning": {"effort": "low"}})
        ),
    ),
    "flux_mixed": (
        {
            "stt": {
                "model": "flux-general-en",
                "failover": [{"provider": "deepgram", "model": "nova-3-general"}],
            }
        },
        "stt.failover",
        MIXED_FLUX_GROUP,
    ),
    "eleven_extra": (
        {**_ELEVEN, "s2s": {"provider": "elevenlabs", "extra": {"eagerness": "auto"}}},
        "s2s",
        eleven_realtime.refusal(
            S2SConfig(provider="elevenlabs", extra={"eagerness": "auto"})
        ),
    ),
    "eleven_duration": (
        {**_ELEVEN, "max_call_duration_seconds": 7201},
        "max_call_duration_seconds",
        eleven_realtime.duration_refusal(7201),
    ),
    "openai_gateway_to_elevenlabs": (
        {
            "pipeline_mode": "s2s",
            "s2s": {
                "provider": "openai",
                "base_url": "wss://api.elevenlabs.io/v1/convai/realtime",
            },
        },
        "s2s",
        eleven_realtime.gateway_refusal(
            S2SConfig(
                provider="openai",
                base_url="wss://api.elevenlabs.io/v1/convai/realtime",
            )
        ),
    ),
}


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Every WARNING the app logs, with its extras."""
    lines: list[str] = []
    sink = logger.add(
        lambda m: lines.append(str(m)), level="WARNING", format="{message} {extra}"
    )
    yield lines
    logger.remove(sink)


def _refused(warnings: list[str], origin: str) -> str:
    [refused] = [w for w in warnings if f"agent_refused origin={origin}" in w]
    return refused


# -- the build's former rules, refused at call-init ---------------------------


@pytest.mark.parametrize(
    ("change", "field", "reason"),
    list(FORMER_BUILD_RULES.values()),
    ids=list(FORMER_BUILD_RULES),
)
async def test_a_rule_the_build_held_refuses_the_inline_agent_at_call_init(
    monkeypatch, redis_env, warnings, change, field, reason
) -> None:
    assert reason  # the rule itself refuses the change
    inline = {**HARNESS_AGENT, "system_prompt": "Never runs.", **change}
    async with (
        CallInitServer({"agent": inline}) as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _route(app, "twilio", server.url)
        await _ring_refused(app, "twilio", number)
        calls = await _calls(app)
        slots = await _live_calls(app.project_id)

    assert calls == []  # refused before a call row, never failed at build
    assert slots == 0
    refused = _refused(warnings, "call_init")
    assert f"field={field}: {reason}" in refused, refused


# -- a stored agent saved before a rule, on each path to the build ------------


async def test_a_webrtc_connect_naming_a_refused_stored_agent_is_a_422(
    monkeypatch,
    redis_env,
    no_peer,  # noqa: F811 - the imported fixture
    warnings,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        broken = await _stored_before_the_rule(app)
        r = await app.api.post(
            "/v1/webrtc/connect",
            json={"sdp": "v=0", "type": "offer", "agent_id": broken},
        )
        calls = await _calls(app)
        slots = await _live_calls(app.project_id)

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "invalid_agent_config"
    assert body["details"] == {"reason": XAI_REASON, "field": "llm"}
    assert calls == []
    assert slots == 0
    assert XAI_REASON in _refused(warnings, "stored_call")


async def test_a_webrtc_connect_naming_no_agent_of_the_project_is_still_a_404(
    monkeypatch,
    no_peer,  # noqa: F811 - the imported fixture
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        r = await app.api.post(
            "/v1/webrtc/connect",
            json={"sdp": "v=0", "type": "offer", "agent_id": str(uuid.uuid4())},
        )

    assert r.status_code == 404, r.text


async def test_a_whatsapp_number_bound_to_a_refused_stored_agent_rejects_the_call(
    monkeypatch,
    redis_env,
    whatsapp_voice,  # noqa: F811 - the imported fixture
    warnings,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await _stored_before_the_rule(app))
        await _enable_whatsapp(number)
        outcome = await _connect_whatsapp(app, number)

    assert_whatsapp_rejected(whatsapp_voice, outcome)
    assert XAI_REASON in _refused(warnings, "stored_call")


async def test_an_outbound_dials_own_refused_agent_fails_the_call_at_its_stream(
    monkeypatch, redis_env, warnings
) -> None:
    """Nothing accepts a dial's own agent at the dial; the media stream does,
    since the build takes nothing else. The call fails with the reason, as a
    build that raised did, and no pipeline is built."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        broken = await _stored_before_the_rule(app)
        number = await app.bind_number(await app.create_agent(HARNESS_AGENT))
        numbers = (await app.api.get("/v1/phone-numbers")).json()["data"]
        number_id = next(n["id"] for n in numbers if n["e164_number"] == number)
        dialled = await app.api.post(
            "/v1/calls/outbound",
            json={
                "agent_id": broken,
                "from_number_id": number_id,
                "to_number": "+15550007777",
            },
        )
        assert dialled.status_code == 201, dialled.text
        call_id = dialled.json()["data"]["id"]
        await answer_outbound_call(
            app.carrier, app.api, await app.carrier.wait_for("dial")
        )
        ended = await app.sink.wait_for("call.ended")
        events = (
            await app.api.get(
                f"/v1/calls/{call_id}/events", params={"event_type": "error.raised"}
            )
        ).json()["data"]
        slots = await _live_calls(app.project_id)

    assert ended["call_id"] == call_id
    assert ended["payload"]["ended_reason"] == "pipeline_error"
    [raised] = events
    assert raised["payload"]["stage"] == "pipeline_build"
    assert XAI_REASON in raised["payload"]["error"]
    assert slots == 0
    assert XAI_REASON in _refused(warnings, "stored_call")


async def test_an_inline_agent_refused_at_its_stream_fails_the_call(
    monkeypatch, redis_env, warnings
) -> None:
    """A call opened with an inline agent its stream no longer accepts (a
    rule newer than the call-init that answered). The call has no agent row
    (ADR-0017) and is failed with the reason all the same: nothing is built,
    the slot is freed, `call.ended` still goes out."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call_id = await _inline_call_row(app.project_id, XAI_BROKEN)
        await _open_twilio_stream(app, call_id)
        ended = await app.sink.wait_for("call.ended")
        events = (
            await app.api.get(
                f"/v1/calls/{call_id}/events", params={"event_type": "error.raised"}
            )
        ).json()["data"]
        slots = await _live_calls(app.project_id)

    assert ended["call_id"] == str(call_id)
    assert ended["agent_id"] is None
    assert ended["payload"]["ended_reason"] == "pipeline_error"
    [raised] = events
    assert XAI_REASON in raised["payload"]["error"]
    assert slots == 0
    assert XAI_REASON in _refused(warnings, "call_init")


XAI_BROKEN = {**HARNESS_AGENT, "llm": {"provider": "xai", "base_url": "https://x"}}


async def _inline_call_row(project_id: str, config: dict) -> uuid.UUID:
    """A Twilio call as call-init left it: an inline agent's row, its slot
    taken."""
    from turncall.domain.enums import CallDirection
    from turncall.services import limits
    from turncall.storage.database import create_session_factory, get_engine
    from turncall.storage.repositories import call_repo

    project, call_id = uuid.UUID(project_id), uuid.uuid4()
    async with create_session_factory(get_engine())() as session:
        await call_repo.create_call(
            session,
            project_id=project,
            call_id=call_id,
            direction=CallDirection.INBOUND,
            provider_call_sid="CAinline",
            metadata_json={"dynamic_config": config},
        )
        await session.commit()
    assert await limits.claim_live_call(project, call_id, 1)
    return call_id


async def _open_twilio_stream(app, call_id: uuid.UUID) -> None:
    """Twilio opening the call's media stream, with its token, until the app
    closes it."""
    import json

    import websockets

    from turncall.services.media_token import media_token

    params = {"callId": str(call_id), "mediaToken": media_token(call_id)}
    start = {"streamSid": "MZ1", "callSid": "CAinline", "customParameters": params}
    url = app.base_url.replace("http", "ws") + "/ws/media-stream"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(json.dumps({"event": "start", "start": start}))
        with contextlib.suppress(websockets.ConnectionClosed, TimeoutError):
            while True:
                await asyncio.wait_for(ws.recv(), 5)
