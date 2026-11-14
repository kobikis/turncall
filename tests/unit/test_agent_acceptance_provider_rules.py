"""Agent acceptance's provider rules (#474, #481): refused with the API's
messages, on whichever origin the config arrives."""

from typing import Any

import pytest

from tests.acceptance_support import accepted as _accepted
from tests.acceptance_support import refused as _refused
from turncall.orchestrator.deepgram_flux import MIXED_FLUX_GROUP

pytestmark = pytest.mark.unit

BROKEN_GRAPH = {
    "name": "billing",
    "steps": [
        {"id": "a", "kind": "ask", "start": True, "moves": [{"to": "missing"}]},
        {"id": "b", "kind": "end"},
    ],
}


# --- provider rules, with the API's messages ------------------------------------


@pytest.mark.parametrize(
    ("llm", "field", "reason"),
    [
        (
            {"provider": "xai", "base_url": "https://gw.example/v1"},
            "llm",
            "base_url is not supported for the xai provider",
        ),
        (
            {"provider": "xai", "reasoning_effort": "minimal"},
            "llm",
            "reasoning_effort 'minimal' is not an xAI value (low|medium|high)",
        ),
        (
            {
                "provider": "openai",
                "failover": [{"provider": "xai", "base_url": "https://gw.example"}],
            },
            "llm.failover[0]",
            "base_url is not supported for the xai provider",
        ),
    ],
)
async def test_an_xai_llm_rule_is_refused(
    llm: dict[str, Any], field: str, reason: str
) -> None:
    refusal = await _refused({"llm": llm})

    assert (refusal.field, refusal.reason) == (field, reason)


async def test_an_xai_llm_on_an_s2s_agent_that_builds_no_llm_is_not_refused() -> None:
    """Nothing builds it, so it fails nothing today (#474 story 29)."""
    await _accepted(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai"},
            "llm": {"provider": "xai", "base_url": "https://gw.example"},
        }
    )


async def test_an_xai_llm_as_gpt_live_1s_delegate_is_refused() -> None:
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai_live"},
            "tools": [
                {"name": "end_call", "description": "End", "parameters_schema": {}}
            ],
            "llm": {"provider": "xai", "reasoning_effort": "minimal"},
        }
    )

    assert refusal.field == "llm"


@pytest.mark.parametrize(
    ("tts", "field"),
    [
        ({"provider": "xai", "speed": 2.0}, "tts"),
        (
            {"provider": "deepgram", "failover": [{"provider": "xai", "speed": 0.5}]},
            "tts.failover[0]",
        ),
    ],
)
async def test_an_xai_tts_speed_out_of_range_is_refused(
    tts: dict[str, Any], field: str
) -> None:
    refusal = await _refused({"tts": tts})

    assert refusal.field == field
    assert refusal.reason == "tts.speed on xai must be between 0.7 and 1.5"


@pytest.mark.parametrize(
    ("s2s", "reason"),
    [
        (
            {"provider": "xai", "temperature": 0.5},
            "s2s.temperature is not supported for the xai S2S provider: Grok "
            "Voice's session has no temperature; remove s2s.temperature",
        ),
        (
            {"provider": "xai", "max_tokens": 200},
            "s2s.max_tokens is not supported for the xai S2S provider: Grok "
            "Voice's session has no output token limit; remove s2s.max_tokens",
        ),
    ],
)
async def test_a_grok_voice_sampling_field_is_refused(
    s2s: dict[str, Any], reason: str
) -> None:
    refusal = await _refused({"pipeline_mode": "s2s", "s2s": s2s})

    assert (refusal.field, refusal.reason) == ("s2s", reason)


async def test_a_grok_voice_base_url_is_refused_pointing_at_gateway_mode() -> None:
    refusal = await _refused(
        {"pipeline_mode": "s2s", "s2s": {"provider": "xai", "base_url": "wss://gw"}}
    )

    assert refusal.field == "s2s"
    assert "gateway mode" in refusal.reason


async def test_a_bad_grok_voice_extra_is_refused() -> None:
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "xai", "extra": {"reasoning": {"effort": "max"}}},
        }
    )

    assert refusal.reason.startswith("s2s.extra.reasoning is not a valid")


async def test_an_s2s_rule_on_a_cascade_agent_is_not_refused() -> None:
    """The build makes no realtime service for a cascade agent."""
    await _accepted({"s2s": {"provider": "xai", "temperature": 0.5}})


async def test_deepgram_flux_mixed_with_another_stt_is_refused() -> None:
    refusal = await _refused(
        {
            "stt": {
                "provider": "deepgram",
                "model": "flux-general-en",
                "failover": [{"provider": "deepgram", "model": "nova-3-general"}],
            }
        }
    )

    assert (refusal.field, refusal.reason) == ("stt.failover", MIXED_FLUX_GROUP)


async def test_an_eleven_realtime_session_longer_than_eleven_allows_is_refused() -> (
    None
):
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "elevenlabs"},
            "max_call_duration_seconds": 9000,
        }
    )

    assert refusal.field == "max_call_duration_seconds"
    assert "Eleven Realtime session" in refusal.reason


async def test_an_eleven_realtime_base_url_is_refused() -> None:
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "elevenlabs", "base_url": "wss://gw"},
        }
    )

    assert refusal.field == "s2s"


async def test_an_openai_gateway_aimed_at_elevenlabs_is_refused() -> None:
    refusal = await _refused(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai", "base_url": "wss://api.elevenlabs.io/x"},
        }
    )

    assert refusal.field == "s2s"
    assert "ElevenLabs" in refusal.reason
