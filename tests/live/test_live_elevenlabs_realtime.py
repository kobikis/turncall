"""Eleven Realtime, the `elevenlabs` S2S provider, against ElevenLabs (#465).

Nothing in the unit suite opens a socket, so these are what say the key is
taken as `xi-api-key`, that ElevenLabs accepts the session TurnCall sends
(temperature, session length, language, semantic VAD), that a refused key's
4001 close reads as `key_rejected`, and that the agent greets from the empty
context and calls a tool.

Live: needs ELEVENLABS_API_KEY on a workspace Eleven Realtime has reached.
Skipped without it.
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.conftest import creds

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.orchestrator.s2s_config import create_s2s_service

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_RUNS = 3
_PROMPT = "You are the receptionist at Luigi's restaurant."


def _blob(**s2s: Any) -> dict[str, Any]:
    return {
        "pipeline_mode": "s2s",
        "system_prompt": _PROMPT,
        "s2s": {"provider": "elevenlabs", **s2s},
    }


async def _connect(service: Any) -> Any:
    import websockets

    return await websockets.connect(
        service.base_url, additional_headers=await service._auth_headers()
    )


async def test_elevenlabs_accepts_the_session_turncall_sends(
    elevenlabs_key: str,
) -> None:
    from pipecat.services.openai.realtime import events

    config = AgentConfig.model_validate(
        _blob(temperature=0.5, extra={"language": "en", "eagerness": "high"})
    )
    service = create_s2s_service(config, creds(elevenlabs=elevenlabs_key))
    async with await _connect(service) as ws:
        service._ws_send = lambda message: ws.send(json.dumps(message))
        await service.send_client_event(
            events.SessionUpdateEvent(session=service._settings.session_properties)
        )

        async def _updated() -> dict[str, Any]:
            async for raw in ws:
                event = json.loads(raw)
                if event.get("type") == "session.updated":
                    return event["session"]
                if event.get("type") == "error":
                    raise AssertionError(event)
            raise AssertionError("socket closed before session.updated")

        session = await asyncio.wait_for(_updated(), timeout=20)
    assert session.get("max_duration_seconds") == 3630, session


async def test_a_refused_key_is_read_as_a_rejected_key(elevenlabs_key: str) -> None:
    """ElevenLabs accepts the socket, then closes it with 4001.
    (`elevenlabs_key` only gates the file: the key used here is wrong.)"""
    from pipecat.utils.errors import ErrorCategory

    from turncall.orchestrator.call_session import provider_of

    config = AgentConfig.model_validate(_blob())
    service = create_s2s_service(config, creds(elevenlabs="not-a-real-key"))
    service.push_frame = AsyncMock()
    service._websocket = await _connect(service)

    await asyncio.wait_for(service._receive_task_handler(), timeout=20)

    (error,) = [c.args[0] for c in service.push_frame.await_args_list]
    assert error.category is ErrorCategory.AUTHENTICATION, error.error
    assert provider_of(service) == "elevenlabs"


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def _run(blob: dict[str, Any], definition: dict, session_factory: Any):
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.TEXT),
        name="live-elevenlabs",
    )
    return await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.TEXT,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=None,
    )


async def test_the_agent_opens_the_call_unprompted(
    elevenlabs_key: str, session_factory: Any
) -> None:
    """No opening-role entry, so the session opens on the empty context, as
    OpenAI Realtime's and Grok's do. Several runs: "sometimes" is the bug."""
    blob = {**_blob(), "first_message": "Thanks for calling Luigi's, this is Marco."}
    definition = {
        "turns": [{"expect": [{"event": "llm_response", "within_ms": 20000}]}]
    }
    greetings = []
    for _ in range(_RUNS):
        result = await _run(blob, definition, session_factory)
        said = " ".join(
            e.get("text") or ""
            for e in result.events_seen
            if e["type"] == "llm_response"
        ).strip()
        assert result.passed and said, [str(f) for f in result.failures]
        greetings.append(said)
    assert all("Marco" in g for g in greetings), greetings


async def test_the_agent_calls_a_tool(
    elevenlabs_key: str, session_factory: Any
) -> None:
    blob = {
        **_blob(model="eleven_realtime_v1_max"),
        "system_prompt": f"{_PROMPT} Use book_table when asked to book.",
        "tools": [
            {
                "name": "book_table",
                "description": "Book a table for the caller on a given day.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {"day": {"type": "string"}},
                    "required": ["day"],
                },
                # Nothing answers it: the call still fires `function_call`.
                "webhook_url": "http://127.0.0.1:9/never",
                "timeout_seconds": 2,
                "max_retries": 0,
            }
        ],
    }
    definition = {
        "turns": [
            {
                "user": "Please book me a table for Friday.",
                "expect": [
                    {"event": "function_call", "name": "book_table", "within_ms": 30000}
                ],
            }
        ]
    }
    result = await _run(blob, definition, session_factory)
    assert result.passed, [str(f) for f in result.failures]
