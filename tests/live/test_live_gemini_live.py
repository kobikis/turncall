"""C6 — Gemini Live under google-genai 2.x.

Pipecat 1.11 floors the `google` extra at `google-genai>=2.19.0`, so taking it
moved the SDK beneath Gemini Live across a major version (1.75 → 2.24). This is
the same shape as the openai 3 upgrade this suite was started for: nothing in
the unit suite opens a socket, so an S2S transport can break under us without a
single test going red.

The model is pinned here rather than read from `S2SConfig`, deliberately — see
`test_a_default_google_agent_names_a_gemini_model`.
"""

import asyncio

import pytest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

# The Live model pipecat 1.11 added support for, and one the API actually
# serves: `gemini-2.0-flash-live-001` is retired and now closes the socket with
# 1008 `not supported for bidiGenerateContent`.
_LIVE_MODEL = "gemini-3.8-live"


async def test_the_genai_sdk_is_the_major_pipecat_requires() -> None:
    """Pipecat 1.11's `google` extra requires `>=2.19.0,<3`. Which major is
    resolved decides whether the rest of this file tests what we think it
    does."""
    import importlib.metadata as md

    version = md.version("google-genai")
    major, minor = (int(part) for part in version.split(".")[:2])

    assert major == 2, f"google-genai {version} is not the 2.x line pipecat 1.11 needs"
    assert (major, minor) >= (2, 19), f"google-genai {version} is below pipecat's floor"


async def test_the_live_socket_opens_and_answers(google_key: str) -> None:
    """The websocket S2S calls ride. Opening it and getting a spoken turn back
    is what proves the 2.x client actually talks to the Live API, rather than
    merely importing.

    Audio out, not text: `gemini-3.8-live` is a native-audio model and closes
    the socket with 1007 for a TEXT modality. Output transcription is enabled
    so the assertion can be about what was said, not just that bytes arrived.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=google_key)
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    audio = bytearray()
    said = ""
    async with client.aio.live.connect(model=_LIVE_MODEL, config=config) as session:
        await session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text="Say only the word: pineapple")],
            ),
            turn_complete=True,
        )

        async def _drain() -> None:
            nonlocal said
            async for message in session.receive():
                if message.data:
                    audio.extend(message.data)
                content = message.server_content
                if not content:
                    continue
                if content.output_transcription and content.output_transcription.text:
                    said += content.output_transcription.text
                if content.turn_complete:
                    return

        await asyncio.wait_for(_drain(), timeout=30)

    assert len(audio) > 1000, f"Live returned {len(audio)} bytes of audio"
    assert "pineapple" in said.lower(), said


async def test_pipecat_builds_a_gemini_live_service(google_key: str) -> None:
    """Construction is where an SDK major lands first: pipecat's service holds
    a `google-genai` client and its settings objects. This exercises the seam
    TurnCall actually calls, not the raw SDK."""
    from turncall.domain.models import AgentConfig, S2SConfig
    from turncall.orchestrator.s2s_config import create_s2s_service

    config = AgentConfig(
        name="gemini-live-probe",
        system_prompt="You are terse.",
        pipeline_mode="s2s",
        s2s=S2SConfig(provider="google", model=_LIVE_MODEL, voice="Puck"),
    )

    service = create_s2s_service(config, openai_api_key="", google_api_key=google_key)

    assert service is not None
    assert type(service).__name__ == "GeminiLiveLLMService"


@pytest.mark.xfail(
    strict=True,
    reason="known gap: _create_gemini_live does not swap the sentinel model; "
    "fix is its own change, not part of the 1.11 upgrade",
)
async def test_a_default_google_agent_names_a_gemini_model() -> None:
    """`S2SConfig.model` defaults to `gpt-realtime-2.1` for every provider. The
    aws and openai_live paths swap that sentinel for a model of their own; the
    google path does not, so an agent that sets `provider: google` and no model
    sends OpenAI's model name to Gemini.

    Marked `xfail(strict=True)`: it records the gap without reddening CI, and
    turns into a failure the moment someone fixes it — which is the prompt to
    delete the marker.
    """
    from turncall.domain.models import S2SConfig

    default_model = S2SConfig(provider="google").model

    assert "gemini" in default_model, (
        f"a default google S2S agent still names {default_model!r}; "
        "_create_gemini_live passes s2s.model through without a swap"
    )
