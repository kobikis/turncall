"""Deepgram Flux against Nova-3 + Smart Turn, measured (#204).

Flux's case is latency: it decides end-of-turn itself, where a Nova-3 agent
waits for VAD and then Smart Turn. Whether that is faster on TurnCall's own
pipeline is a measurement, not a claim — so the same scenario runs against
both, and each turn's `metrics.turn` record is collected.

Audio modality over the eval loopback: the harness paces the caller's
synthesized speech in real time, 40ms a frame with silence between turns, which
is what VAD, Smart Turn and Flux all key off. An eval writes no call events
(ADR-0018), so the recorder's write is swapped for a list here; everything that
produces the record is the real one.

`turn_end_ms` is the number Flux moves; `user_to_bot_ms` is what the caller
waits, LLM and TTS included. Both are reported (p50, and how many turns lacked a
`turn_end_ms` — one released before VAD's 0.2s window closed has none) whether
the assertion holds or not.

Needs DEEPGRAM_API_KEY (both STTs, and the agent's TTS) and OPENAI_API_KEY (the
agent's LLM). The first run downloads Kokoro and Moonshine (`~/.cache/pipecat`).
"""

import statistics
from typing import Any
from uuid import uuid4

import pytest
from loguru import logger

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.orchestrator import turn_metrics

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_ROUNDS = 3
_SCENARIO = {
    "turns": [
        {
            "user": "What is the capital of France? Answer with just the city.",
            "expect": [
                {"event": "response", "text_contains": "Paris", "within_ms": 30000}
            ],
        },
        {
            "user": "And the capital of Italy? Again, just the city.",
            "expect": [
                {"event": "response", "text_contains": "Rome", "within_ms": 30000}
            ],
        },
    ]
}
_NOVA = {"provider": "deepgram", "model": "nova-3-general"}
_FLUX = {"provider": "deepgram", "model": "flux-general-en"}


@pytest.fixture
def session_factory() -> Any:
    """Only `build_call_pipeline`'s KB lookup uses this; an eval writes nothing."""
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `metrics.turn` payload the pipeline produces, in order."""
    payloads: list[dict[str, Any]] = []

    async def write(payload: dict[str, Any]) -> None:
        payloads.append(payload)

    monkeypatch.setattr(
        turn_metrics,
        "turn_metrics_observers",
        lambda ctx: [turn_metrics.TurnMetricsRecorder(ctx, write=write).observer],
    )
    return payloads


async def _converse(stt: dict, session_factory: Any) -> Any:
    blob = {
        "system_prompt": "You are a terse geography assistant.",
        "first_message": None,
        "llm": {"provider": "openai", "model": "gpt-4o-mini"},
        "stt": stt,
        "tts": {"provider": "deepgram"},
        # Nova's baseline is the default: Smart Turn on. Ignored on Flux.
        "smart_turn_detection": True,
    }
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(_SCENARIO, EvalModality.AUDIO), name="live-flux"
    )
    return await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=ResolvedTarget(
            project_id=uuid4(),
            config=AgentConfig.model_validate(blob),
            config_blob=blob,
            agent_id=None,
        ),
        modality=EvalModality.AUDIO,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=None,
    )


async def _measure(stt: dict, session_factory: Any, recorded: list) -> dict[str, Any]:
    start = len(recorded)
    for _ in range(_ROUNDS):
        result = await _converse(stt, session_factory)
        assert result.passed, [str(f) for f in result.failures]
    turns = recorded[start:]
    ends = [t["turn_end_ms"] for t in turns if "turn_end_ms" in t]
    return {
        "turns": len(turns),
        "turn_end_p50_ms": statistics.median(ends) if ends else None,
        "turn_end_missing": len(turns) - len(ends),
        "user_to_bot_p50_ms": statistics.median(t["user_to_bot_ms"] for t in turns),
    }


async def test_an_agent_on_flux_completes_a_conversation(
    openai_key: str, deepgram_key: str, session_factory: Any, recorded: list
) -> None:
    """Heard through Flux, answered, and every turn measured."""
    result = await _converse(_FLUX, session_factory)

    assert result.passed, [str(f) for f in result.failures]
    assert len(recorded) == len(_SCENARIO["turns"]), recorded
    assert all("user_to_bot_ms" in t for t in recorded), recorded
    logger.info("flux_turns {turns}", turns=recorded)


async def test_flux_ends_the_turn_sooner_than_nova3_with_smart_turn(
    openai_key: str, deepgram_key: str, session_factory: Any, recorded: list
) -> None:
    nova = await _measure(_NOVA, session_factory, recorded)
    flux = await _measure(_FLUX, session_factory, recorded)

    # The measurement is the point, so it is reported whatever the verdict.
    logger.info(
        "flux_vs_nova3_smart_turn nova={nova} flux={flux}", nova=nova, flux=flux
    )
    assert flux["user_to_bot_p50_ms"] < nova["user_to_bot_p50_ms"], (
        "Flux did not answer sooner than Nova-3 + Smart Turn on this scenario: "
        f"nova={nova} flux={flux}. #204's latency claim does not hold here."
    )
