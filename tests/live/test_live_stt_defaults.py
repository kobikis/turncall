"""C9 — every STT provider's default model is one that provider serves.

`_STT_DEFAULTS` names a model per provider, used whenever an agent sets none.
Nothing hermetic can tell whether those names are real: the unit tests assert
the resolver returns the string in the table, and the table is the thing that
could be wrong. It already was — `nova-3-general` sat there for all four
providers, and three of them answer 400 to it.

Model names also rot. `gemini-2.0-flash-live-001` was a reasonable-looking
choice that stopped being served (see C6), and a retired default breaks every
agent that never named a model — the ones least likely to be watching.
"""

import io
import math
import os
import struct
import wave

import httpx
import pytest

from turncall.orchestrator.pipeline_factory import _STT_DEFAULTS

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def _one_second_of_tone() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * i / 16000)))
                for i in range(16000)
            )
        )
    return buffer.getvalue()


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is not set")
    return value


async def _post(url: str, *, headers: dict, data: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=60) as client:
        return await client.post(
            url,
            headers=headers,
            files={"file": ("probe.wav", _one_second_of_tone(), "audio/wav")},
            data=data,
        )


async def test_deepgram_serves_its_default() -> None:
    key = _require("DEEPGRAM_API_KEY")
    model = _STT_DEFAULTS["deepgram"]

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"https://api.deepgram.com/v1/listen?model={model}",
            headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
            content=_one_second_of_tone(),
        )

    assert response.status_code == 200, f"{model}: {response.text[:200]}"


async def test_openai_serves_its_default() -> None:
    key = _require("OPENAI_API_KEY")
    model = _STT_DEFAULTS["openai"]

    response = await _post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        data={"model": model},
    )

    assert response.status_code == 200, f"{model}: {response.text[:200]}"


async def test_elevenlabs_serves_its_default() -> None:
    key = _require("ELEVENLABS_API_KEY")
    model = _STT_DEFAULTS["elevenlabs"]

    response = await _post(
        "https://api.elevenlabs.io/v1/speech-to-text",
        headers={"xi-api-key": key},
        data={"model_id": model},
    )

    assert response.status_code == 200, f"{model}: {response.text[:200]}"


async def test_cartesia_serves_its_default() -> None:
    key = _require("CARTESIA_API_KEY")
    model = _STT_DEFAULTS["cartesia"]

    response = await _post(
        "https://api.cartesia.ai/stt",
        headers={"X-API-Key": key, "Cartesia-Version": "2024-06-10"},
        data={"model": model},
    )

    assert response.status_code == 200, f"{model}: {response.text[:200]}"
