"""The model's stage directions are never spoken.

Told the call ends after its next reply, gpt-4o-mini wrote
"Goodbye! [Ending the call...]" and "*Ending the call now.*" on the website
demo, and the voice read both aloud. Each provider's TTS carries a filter
that drops them, judged sentence by sentence as pipecat's aggregator hands
them over — so the cases below go through that aggregator too.
"""

import pytest
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from tests.conftest import creds
from turncall.domain.models import AgentConfig, TTSConfig
from turncall.orchestrator.pipeline_factory import _create_tts_service
from turncall.orchestrator.stage_directions import (
    StageDirectionFilter,
    strip_stage_directions,
    strip_voice_tags,
)


async def _spoken(reply: str) -> list[str]:
    """What reaches the voice: the reply streamed a few characters at a time,
    aggregated into sentences, filtered, empties dropped (as pipecat does)."""
    aggregator, sentences = SimpleTextAggregator(), []
    for i in range(0, len(reply), 3):
        async for aggregation in aggregator.aggregate(reply[i : i + 3]):
            sentences.append(aggregation.text)
    rest = await aggregator.flush()
    if rest:
        sentences.append(rest.text)
    spoken = [await StageDirectionFilter().filter(s) for s in sentences]
    return [s for s in spoken if s.strip()]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reply", "spoken"),
    [
        (
            "You're welcome! Have a great day. Goodbye! [Ending the call...]",
            ["You're welcome!", "Have a great day.", "Goodbye!"],
        ),
        ("*Ending the call now.*", []),
        ("Goodbye! *Ending the call now.* Take care!", ["Goodbye!", "Take care!"]),
        ("Bye now! [Call ended.]", ["Bye now!"]),
    ],
)
async def test_a_narration_is_not_spoken(reply, spoken):
    assert await _spoken(reply) == spoken


@pytest.mark.unit
@pytest.mark.parametrize(
    "sentence",
    [
        "I *really* can help.",
        "*Really* good.",
        "We're open 8 to 6 (Monday to Friday).",
        "5 * 3 is 15.",
        " Thanks for calling,  this is Maya!",
    ],
)
def test_ordinary_text_is_left_exactly_as_it_was(sentence):
    assert strip_stage_directions(sentence) == sentence


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider", ["deepgram", "elevenlabs", "openai", "cartesia", "xai"]
)
def test_every_tts_provider_carries_the_filter(provider, monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "x")
    service = _create_tts_service(
        AgentConfig(tts=TTSConfig(provider=provider)), credentials=creds("x")
    )
    assert any(isinstance(f, StageDirectionFilter) for f in service._text_filters)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "plain"),
    [
        ('<emotion value="excited"/>Oh wow, that is great!', "Oh wow, that is great!"),
        ('Sure. <break time="1s"/> Done.', "Sure. Done."),
        ("Your code is <spell>A1B</spell>.", "Your code is A1B."),
        ("Nothing to strip <here>.", "Nothing to strip <here>."),
    ],
)
def test_voice_tags_come_out(text, plain):
    assert strip_voice_tags(text) == plain


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "spoken"),
    [
        ("cartesia", '<emotion value="excited"/>Oh wow!'),
        ("deepgram", "Oh wow!"),
        ("elevenlabs", "Oh wow!"),
        ("openai", "Oh wow!"),
        ("xai", "Oh wow!"),
    ],
)
async def test_only_cartesia_hears_its_tags(provider, spoken, monkeypatch):
    """Sonic-3 acts on them; any other voice, a Deepgram backup in the same
    failover group included, would read them out."""
    monkeypatch.setenv("CARTESIA_API_KEY", "x")
    service = _create_tts_service(
        AgentConfig(tts=TTSConfig(provider=provider)), credentials=creds("x")
    )
    [text_filter] = service._text_filters
    assert await text_filter.filter('<emotion value="excited"/>Oh wow!') == spoken


@pytest.mark.unit
async def test_strip_markdown_leaves_the_tags_for_the_voice():
    """The markdown transform runs on Cartesia's text too; it must not eat the
    tag the filter kept."""
    from pipecat.utils.text.transforms import strip_markdown

    tagged = '<emotion value="excited"/>That is **great**!'
    assert await strip_markdown(tagged, "sentence") == (
        '<emotion value="excited"/>That is great!'
    )
