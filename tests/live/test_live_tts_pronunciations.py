"""The pronunciation markup still changes what Deepgram synthesizes (#140).

`tts.pronunciations` is one IPA dictionary that each provider renders in its
own markup — Deepgram's inline `\\{"word": ..., "pronounce": ...\\}` object here.
Whether that markup is still *read* is the vendor's, and nothing hermetic can
notice it changing: `format_pronunciation` would keep returning what it always
returned and every unit test would keep passing while the audio stopped caring.

Deliberately not asserted: *how* the audio differs. That is a vendor's acoustic
output, not our contract. Different bytes for the same sentence is the whole
claim — the markup reached the model and the model acted on it.
"""

import httpx
import pytest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_SPEAK = "https://api.deepgram.com/v1/speak?model=aura-2-helena-en"
_SENTENCE = "Take your {word} today"
_WORD = "Metformin"
_IPA = "mɛtˈfɔɹmɪn"


async def _speak(key: str, text: str) -> bytes:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            _SPEAK,
            headers={
                "Authorization": f"Token {key}",
                "Content-Type": "application/json",
            },
            json={"text": text},
        )
    assert response.status_code == 200, response.text[:300]
    return response.content


async def test_the_markup_changes_the_audio(deepgram_key: str) -> None:
    """The one thing our code owns end to end: the markup our transform builds
    from an author's IPA is markup this provider still acts on."""
    from pipecat.services.deepgram.tts import DeepgramTTSService

    markup = DeepgramTTSService.format_pronunciation(_WORD, _IPA)
    assert markup, "Deepgram stopped rendering IPA pronunciations at all"

    plain = await _speak(deepgram_key, _SENTENCE.format(word=_WORD))
    hinted = await _speak(deepgram_key, _SENTENCE.format(word=markup))

    assert plain and hinted
    assert hinted != plain, (
        "identical audio with and without the pronunciation markup — Deepgram "
        "may have stopped reading it, which no hermetic test can see"
    )


# ElevenLabs, measured: two syntheses of the *same* text return different bytes
# even with a fixed seed, so the comparison is on byte length — the duration of
# the same codec stream — and the test calibrates that floor itself. Lengths are
# stable when seeded, and a subtly respelled word can land on the same duration
# (it does, on two of three voices), so the pronunciation under test is
# deliberately an unmistakable one: the claim is that the markup reached the
# model, not that any particular respelling is audible.
_ELEVENLABS = "https://api.elevenlabs.io/v1/text-to-speech"
_ELEVENLABS_MODEL = "eleven_flash_v2"
_ELEVENLABS_IPA = "mɛtəˈfɔːɹmiːniː"
_SEED = 1234


async def _elevenlabs_voice(key: str) -> str:
    """The account's own first voice: a library voice id is 402 on a free plan."""
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            f"{_ELEVENLABS.rsplit('/', 1)[0]}/voices", headers={"xi-api-key": key}
        )
    assert response.status_code == 200, response.text[:300]
    voices = response.json().get("voices", [])
    if not voices:
        pytest.skip("this ElevenLabs account has no voices")
    return str(voices[0]["voice_id"])


async def _speak_elevenlabs(key: str, voice: str, text: str) -> int:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{_ELEVENLABS}/{voice}?enable_ssml_parsing=true",
            headers={"xi-api-key": key},
            json={"text": text, "model_id": _ELEVENLABS_MODEL, "seed": _SEED},
        )
    assert response.status_code == 200, response.text[:300]
    return len(response.content)


async def test_elevenlabs_still_reads_the_phoneme_markup(elevenlabs_key: str) -> None:
    """The other half of #147: SSML parsing is reachable now, and these tags are
    markup this provider still acts on. Not asserted, deliberately: *how* the
    audio differs — that is the vendor's acoustics, not our contract."""
    from pipecat.services.elevenlabs.tts import (
        ELEVENLABS_PHONEME_MODELS,
        ElevenLabsTTSService,
    )

    assert _ELEVENLABS_MODEL in ELEVENLABS_PHONEME_MODELS, "the phoneme set moved"
    markup = ElevenLabsTTSService.format_pronunciation(_WORD, _ELEVENLABS_IPA)
    assert markup, "ElevenLabs stopped rendering IPA pronunciations at all"
    voice = await _elevenlabs_voice(elevenlabs_key)

    plain = _SENTENCE.format(word=_WORD)
    spoken = await _speak_elevenlabs(elevenlabs_key, voice, plain)
    again = await _speak_elevenlabs(elevenlabs_key, voice, plain)
    hinted = await _speak_elevenlabs(
        elevenlabs_key, voice, _SENTENCE.format(word=markup)
    )

    assert spoken == again, (
        f"seeded syntheses of the same sentence differ in length ({spoken} vs "
        f"{again}), so comparing one to the marked-up version concludes nothing"
    )
    assert hinted != spoken, (
        "the same audio length with and without the <phoneme> markup — ElevenLabs "
        "may have stopped reading SSML, which no hermetic test can see"
    )
