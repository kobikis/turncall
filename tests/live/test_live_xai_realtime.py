"""Grok Voice, the `xai` S2S provider, against xAI itself (#386).

Nothing in the unit suite opens a socket, so these are what say the default
model is one xAI serves, that a refused key is a 401 at the handshake (which
`call_session` reads as `key_rejected`), and whether Grok greets from the
empty context every other Realtime-protocol model greets from — the question
#381 answered "no" for Gemini Live and Nova Sonic.

Live: needs XAI_API_KEY. Skipped without it.
"""

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from tests.conftest import creds

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig, S2SConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.orchestrator.s2s_config import create_s2s_service

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_URL = "wss://api.x.ai/v1/realtime"
_RUNS = 5


def _default_agent(first_message: str | None = None) -> AgentConfig:
    return AgentConfig(
        name="grok-default",
        system_prompt="You are the receptionist at Luigi's restaurant.",
        first_message=first_message,
        pipeline_mode="s2s",
        s2s=S2SConfig(provider="xai"),
    )


async def _session_created(api_key: str, model: str) -> dict[str, Any]:
    """Open xAI's realtime socket on `model` and return its session event."""
    import websockets

    async with websockets.connect(
        f"{_URL}?model={model}",
        additional_headers={"Authorization": f"Bearer {api_key}"},
    ) as ws:

        async def _first_session_event() -> dict[str, Any]:
            async for raw in ws:
                event = json.loads(raw)
                if event.get("type") in ("session.created", "session.updated"):
                    return event
                if event.get("type") == "error":
                    raise AssertionError(event)
            raise AssertionError("socket closed before a session event")

        return await asyncio.wait_for(_first_session_event(), timeout=20)


async def test_pipecat_builds_the_service_from_a_default_agent(xai_key: str) -> None:
    service = create_s2s_service(_default_agent(), credentials=creds(xai=xai_key))

    from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService

    assert isinstance(service, GrokRealtimeLLMService)
    assert service._settings.model == "grok-voice-3.0"


async def test_the_socket_opens_on_the_default_model(xai_key: str) -> None:
    """The model the factory swaps in is one xAI actually serves."""
    service = create_s2s_service(_default_agent(), credentials=creds(xai=xai_key))

    event = await _session_created(xai_key, str(service._settings.model))

    assert event["type"] in ("session.created", "session.updated")


async def test_a_refused_key_is_read_as_a_rejected_key(xai_key: str) -> None:
    """xAI refuses a wrong key at the handshake with HTTP 400 "Incorrect API
    key provided", not a 401. The built service still files it as
    AUTHENTICATION, so the call's `error.raised` carries `provider: "xai"` and
    `key_rejected: true`. (`xai_key` only gates the file: the key used here
    is deliberately wrong.)"""
    from unittest.mock import AsyncMock

    from pipecat.utils.errors import ErrorCategory

    from turncall.orchestrator.call_session import provider_of

    service = create_s2s_service(
        _default_agent(), credentials=creds(xai="xai-not-a-real-key")
    )
    service.push_frame = AsyncMock()

    await service._connect()

    (error,) = [c.args[0] for c in service.push_frame.await_args_list]
    assert error.category is ErrorCategory.AUTHENTICATION, error.error
    assert provider_of(service) == "xai"


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


_GREETING = "Thanks for calling Luigi's, this is Marco speaking."


@pytest.mark.parametrize(
    "first_message", [_GREETING, None], ids=["first_message", "no_first_message"]
)
async def test_the_agent_opens_the_call_unprompted(
    xai_key: str, session_factory: Any, first_message: str | None
) -> None:
    """Over the eval loopback, the caller saying nothing: the first turn has
    no `user:`, so it passes only if the agent opens the call by itself. Grok
    has no opening-role entry, so its session opens on the empty context.
    Several runs per config, because a model that greets "sometimes" is the
    bug.

    Text modality, which needs no local speech models: Grok's own words come
    back as `llm_response`. Its text is asserted, never the bare event, since
    pipecat emits an empty one after a provider error."""
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are the receptionist at Luigi's restaurant.",
        "first_message": first_message,
        "s2s": {"provider": "xai"},
    }
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    definition = {
        "turns": [{"expect": [{"event": "llm_response", "within_ms": 20000}]}]
    }
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.TEXT),
        name="live-xai-speaks-first",
    )

    silent, greetings = [], []
    for _ in range(_RUNS):
        result = await run_iteration(
            parsed=parsed,
            kind=EvalKind.SCRIPTED,
            target=target,
            modality=EvalModality.TEXT,
            settings=Settings(),
            session_factory=session_factory,
            run_id=uuid4(),
            tool_mocks=None,
        )
        said = " ".join(
            e.get("text") or ""
            for e in result.events_seen
            if e["type"] == "llm_response"
        ).strip()
        if not result.passed or not said:
            silent.append([str(f) for f in result.failures] or ["no words"])
        greetings.append(said)
    assert not silent, f"{len(silent)}/{_RUNS} runs did not greet: {silent}"

    if first_message:
        missed = [g for g in greetings if "Marco" not in g]
        assert not missed, greetings
