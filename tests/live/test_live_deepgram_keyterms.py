"""C8 — Deepgram still splits vocabulary hints by model.

`STTConfig.keyterms` maps to `keyterm` or `keywords` depending on the Deepgram
model, because Deepgram rejects the wrong one with a 400 rather than ignoring
it: the wrong spelling does not weaken transcription, it ends the call at
connect. That rule is Deepgram's, not ours, and nothing in the unit suite can
notice it changing — `_keyterm_kwargs` would keep returning what it always
returned, and every hermetic test would keep passing.

If Deepgram ever accepts both, these fail and the mapping can collapse to one
spelling. If it moves the boundary — another model family joining Nova-3 and
Flux — the first test fails and names the model that moved.
"""

import io
import math
import struct
import wave

import httpx
import pytest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_LISTEN = "https://api.deepgram.com/v1/listen"


def _one_second_of_tone() -> bytes:
    """A second of 220Hz. Deepgram needs audio to accept the request at all;
    what it transcribes is irrelevant — the query parameter is under test."""
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


async def _listen(key: str, model: str, param: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.post(
            f"{_LISTEN}?model={model}&{param}=Acme",
            headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
            content=_one_second_of_tone(),
        )


@pytest.mark.parametrize(
    ("model", "accepted", "rejected"),
    [
        ("nova-3-general", "keyterm", "keywords"),
        ("nova-2", "keywords", "keyterm"),
    ],
)
async def test_the_model_decides_the_spelling(
    deepgram_key: str, model: str, accepted: str, rejected: str
) -> None:
    """What `_keyterm_kwargs` encodes, asserted against the API it encodes."""
    ok = await _listen(deepgram_key, model, accepted)
    assert ok.status_code == 200, f"{model} rejected {accepted}: {ok.text[:200]}"

    bad = await _listen(deepgram_key, model, rejected)
    assert bad.status_code == 400, (
        f"{model} ACCEPTED {rejected} — Deepgram's two spellings may have "
        f"converged, and _keyterm_kwargs can stop branching on the model"
    )
    assert "INVALID_QUERY_PARAMETER" in bad.text, bad.text[:200]
