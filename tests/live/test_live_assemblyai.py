"""AssemblyAI Universal-Streaming against the real API (#203).

Three claims the unit suite can only take on trust, because each is
AssemblyAI's rule rather than TurnCall's:

- the two default models (`universal-streaming-english`, and the multilingual
  one a non-English agent gets) are still served;
- a session carrying more than 100 keyterms is refused, which is why the
  pipeline factory cuts the list at 100 — if this starts passing 101, the cap
  can go;
- an agent on it hears a caller: an audio-mode eval, whose synthesized
  question reaches the agent only through AssemblyAI.

Needs ASSEMBLYAI_API_KEY; the eval also needs OPENAI_API_KEY (the agent's LLM)
and DEEPGRAM_API_KEY (its TTS).
"""

import asyncio
import json
from urllib.parse import urlencode
from uuid import uuid4

import pytest
import websockets

from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.orchestrator.pipeline_factory import (
    _ASSEMBLYAI_MULTILINGUAL,
    _STT_DEFAULTS,
)

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_STREAMING = "wss://streaming.assemblyai.com/v3/ws"


async def _first_message(key: str, model: str, keyterms: list[str]) -> str:
    """The type of the session's first message, or why there was none."""
    query = urlencode(
        {
            "sample_rate": 16000,
            "encoding": "pcm_s16le",
            "speech_model": model,
            "keyterms_prompt": json.dumps(keyterms),
        }
    )
    try:
        async with websockets.connect(
            f"{_STREAMING}?{query}",
            additional_headers={"Authorization": key},
            open_timeout=15,
        ) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 15))
            await ws.send(json.dumps({"type": "Terminate"}))
            return first.get("type") or f"no type: {first}"
    except websockets.exceptions.InvalidStatus as e:
        return f"refused: HTTP {e.response.status_code}"
    except websockets.exceptions.ConnectionClosed as e:
        return (
            f"closed: {e.rcvd.code if e.rcvd else ''} {e.rcvd.reason if e.rcvd else ''}"
        )


@pytest.mark.parametrize(
    "model", [_STT_DEFAULTS["assemblyai"], _ASSEMBLYAI_MULTILINGUAL]
)
async def test_the_default_models_are_served(assemblyai_key: str, model) -> None:
    assert await _first_message(assemblyai_key, model, ["Acme"]) == "Begin"


async def test_more_than_a_hundred_keyterms_is_refused(assemblyai_key: str) -> None:
    model = _STT_DEFAULTS["assemblyai"]
    at_cap = [f"term{i}" for i in range(100)]
    assert await _first_message(assemblyai_key, model, at_cap) == "Begin"

    over = await _first_message(assemblyai_key, model, [*at_cap, "one-more"])
    assert over != "Begin", (
        "AssemblyAI accepted 101 keyterms — the 100 cap in _keyterm_kwargs "
        "is no longer needed"
    )


@pytest.fixture
def session_factory():
    """Only `build_call_pipeline`'s credential and KB lookups use this."""
    from turncall.config.settings import Settings
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_an_agent_on_assemblyai_hears_the_caller(
    assemblyai_key: str, openai_key: str, deepgram_key: str, session_factory
) -> None:
    from turncall.config.settings import Settings

    blob = {
        "system_prompt": "You are a terse geography assistant.",
        "first_message": None,
        "llm": {"provider": "openai", "model": "gpt-4o-mini"},
        "stt": {"provider": "assemblyai"},
        "tts": {"provider": "deepgram"},
    }
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    definition = {
        "turns": [
            {
                "user": "What is the capital of France? Answer with just the city.",
                "expect": [
                    {"event": "response", "text_contains": "Paris", "within_ms": 30000}
                ],
            }
        ]
    }
    merged = scenario_mod.with_modality(definition, EvalModality.AUDIO)
    errors: list[str] = []
    result = await run_iteration(
        parsed=scenario_mod.parse(merged, name="live-assemblyai"),
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.AUDIO,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=None,
        agent_errors=errors,
    )

    assert result.passed, ([str(f) for f in result.failures], errors)
