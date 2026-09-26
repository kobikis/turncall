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
