"""Audio modality, end to end (#72).

The mode that covers what makes TurnCall a voice platform: the caller's turns
are synthesized and reach the agent's real STT, the agent speaks through its
real TTS, and the judge reads a transcription of what was actually said. Text
modality skips both ends, which is why it could not have caught #64.

Live, and slower than the text tests by an order of magnitude: the first run
downloads Kokoro (the caller's voice) and Moonshine (the STT the judge reads
through) into `~/.cache/pipecat`. Needs OPENAI_API_KEY (the agent's LLM) and
DEEPGRAM_API_KEY (its STT and TTS, both really used here).
"""

import time
from pathlib import Path
from uuid import uuid4

import pytest
from loguru import logger

from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget, map_script_result

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def _target(project_id, prompt: str, **over):
    blob = {
        "system_prompt": prompt,
        "first_message": None,
        "llm": {"provider": "openai", "model": "gpt-4o-mini"},
        "stt": {"provider": "deepgram"},
        "tts": {"provider": "deepgram"},
        **over,
    }
    return ResolvedTarget(
        project_id=project_id,
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )


async def _run(definition: dict, target, session_factory, *, cache_dir=None):
    from turncall.config.settings import Settings

    merged = scenario_mod.with_modality(definition, EvalModality.AUDIO)
    parsed = scenario_mod.parse(merged, name="live-audio")
    settings = Settings()
    if cache_dir is not None:
        settings.evals.tts_cache_dir = str(cache_dir)
    return await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.AUDIO,
        settings=settings,
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=None,
    ), parsed


@pytest.fixture
def session_factory():
    """Only `build_call_pipeline`'s KB lookup uses this; an eval writes nothing."""
    from turncall.config.settings import Settings
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_an_audio_run_goes_through_the_agents_real_stt_and_tts(
    openai_key: str, deepgram_key: str, session_factory
) -> None:
    """The caller is spoken, the agent hears it, answers out loud, and the
    judge reads a transcription of the audio that was actually produced."""
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
    target = _target(uuid4(), "You are a terse geography assistant.")
    result, parsed = await _run(definition, target, session_factory)

    assert result.passed, [str(f) for f in result.failures]
    kinds = {e.get("type") for e in result.events_seen}
    assert "response" in kinds, "the harness transcribed nothing — no bot audio"
    assert "tts_response" in kinds, "the agent's TTS never spoke"

    outcome = map_script_result(result, iteration=1, parsed=parsed)
    reply = next(m for m in outcome.entry["transcript"] if m["role"] == "assistant")
    # What the judge read is the content; the agent's own text rides along
    # whenever the STT heard something different. That difference is the whole
    # explanation when an audio run fails where a text run passed.
    assert "paris" in reply["content"].lower()
    assert "text" not in reply or "Paris" in reply["text"]


async def test_a_latency_budget_fails_a_run_that_exceeds_it(
    openai_key: str, deepgram_key: str, session_factory
) -> None:
    """`within_ms` is the mechanism the #67 class would be caught by: the VAD
    and smart-turn waits are real wall-clock waits in audio mode. A budget no
    real pipeline can meet must fail the run rather than pass it late."""
    definition = {
        "turns": [
            {
                "user": "Say hello.",
                "expect": [{"event": "response", "within_ms": 1}],
            }
        ]
    }
    target = _target(uuid4(), "You are a friendly assistant. Be brief.")
    result, _ = await _run(definition, target, session_factory)

    assert not result.passed, "a 1ms budget cannot have been met"
    assert result.failures, "a missed budget has to say so"


async def test_the_turn_timing_claim_is_measured_not_assumed(
    openai_key: str, deepgram_key: str, session_factory
) -> None:
    """evals-design §1 records the #67 claim as *unverified*: that a latency
    budget catches two silence timers running in series.

    Measured here rather than argued: the same scenario against the same agent
    twice, once with the silence window at its default and once at two seconds
    with smart turn off, which is the shape #67 fixed. If the budget works, the
    slowed agent is slower by roughly that difference, and a budget between the
    two passes one and fails the other.
    """
    definition = {"turns": [{"user": "Say hello.", "expect": [{"event": "response"}]}]}
    prompt = "You are a friendly assistant. Reply with one short sentence."

    fast, _ = await _run(
        definition,
        _target(uuid4(), prompt, smart_turn_detection=False, silence_timeout_ms=200),
        session_factory,
    )
    slow, _ = await _run(
        definition,
        _target(uuid4(), prompt, smart_turn_detection=False, silence_timeout_ms=2000),
        session_factory,
    )

    assert fast.passed and slow.passed, "both should answer; only the timing differs"
    fast_ms, slow_ms = fast.turns[0].duration_ms, slow.turns[0].duration_ms
    gap_ms = slow_ms - fast_ms
    # The measurement is the point of this test, so it is reported whether it
    # passes or fails — a number nobody can read settles nothing.
    logger.info(
        "turn_timing_measurement fast={fast}ms slow={slow}ms gap={gap}ms",
        fast=fast_ms,
        slow=slow_ms,
        gap=gap_ms,
    )
    # The finding this test exists to produce. Recorded in the failure message
    # so a run that disproves it says so out loud rather than just going red.
    assert gap_ms > 800, (
        "the silence window did not show up in the measured turn: "
        f"fast={fast_ms}ms slow={slow_ms}ms. "
        "A latency budget does NOT catch the #67 class — update evals-design §1."
    )


async def test_the_callers_audio_is_synthesized_once_and_reused(
    openai_key: str, deepgram_key: str, session_factory, tmp_path: Path
) -> None:
    """Same scenario, same voice, same words: the second run must not pay to
    synthesize them again."""
    definition = {"turns": [{"user": "Say hello.", "expect": [{"event": "response"}]}]}
    target = _target(uuid4(), "You are a friendly assistant. Be brief.")
    cache = tmp_path / "tts"

    await _run(definition, target, session_factory, cache_dir=cache)
    first = {p: p.stat().st_mtime_ns for p in cache.rglob("*") if p.is_file()}
    assert first, "nothing was cached — a repeat run would re-synthesize"

    started = time.monotonic()
    await _run(definition, target, session_factory, cache_dir=cache)
    second = {p: p.stat().st_mtime_ns for p in cache.rglob("*") if p.is_file()}

    assert second == first, "the cached audio was rewritten instead of reused"
    assert time.monotonic() - started < 120
