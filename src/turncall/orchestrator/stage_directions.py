"""Keep the model's stage directions out of the caller's ear.

Everything an LLM writes in a voice reply is spoken. Told that the call ends
after its next reply, gpt-4o-mini narrates instead of answering:
"Goodbye! [Ending the call...]", or "*Ending the call now.*" when it has
nothing left to say — and the voice read both out on the website demo.

A narration is bracketed text, or a whole sentence in asterisks. Emphasis
(`I *really* can`) has no sentence ending inside its asterisks and is left for
`strip_markdown`, which drops the asterisks and keeps the word.

A TTS text transform, registered ahead of every other: pipecat (1.14) leaves
a chunk a transform empties unspoken, and `strip_markdown` after it would drop
the asterisks a starred sentence is recognised by. (It was a text filter until
pipecat 1.14 deprecated those, when an emptied transform was still sent.)
Stateless, so it judges each sentence the aggregator hands it alone. The
aggregator ends a sentence inside a starred one, so "*Ending the call now.*"
arrives as "*Ending the call now." and then "*": an opening star with no
closing one is the head of a narration, a star with nothing after it its tail.

Cartesia's inline tags (`<emotion value="excited"/>`, `<break time="1s"/>`,
...) are direction too, but for the voice: Sonic-3 acts on them and an agent's
prompt may ask for them. Every other voice would read them out — including a
Deepgram backup in the same failover group — so only Cartesia's filter keeps
them. They never reach the transcript.
"""

import re

from pipecat.utils.text.base_text_aggregator import TextType
from pipecat.utils.text.transforms.text_transform import TextTransform

_BRACKETED = re.compile(r"\[[^\[\]]*\]")
# Asterisks around text that ends a sentence: a narration, not emphasis.
_STARRED_SENTENCE = re.compile(r"\*[^*\n]*[.!?…]\s*\*")
# An unclosed bracket or an opening star (one followed by a non-space) that
# runs to the end of the sentence: a narration the aggregator split.
_OPEN_TO_END = re.compile(r"(\[[^\]]*|(?<!\S)\*(?=\S)[^*]*)$")
# A closing star or bracket left over from the sentence before.
_CLOSE_AT_START = re.compile(r"^\s*(\*(?!\S)|[^\[]*\])")
# Cartesia's SSML-style tags, the set pipecat strips from its word timestamps.
_VOICE_TAG = re.compile(r"<\s*/?\s*(?:emotion|speed|volume|break|spell)\b[^>]*>", re.I)


def _tidy(original: str, stripped: str) -> str:
    if stripped == original:
        return original
    return re.sub(r"\s{2,}", " ", stripped).strip()


def strip_voice_tags(text: str) -> str:
    """`text` without Cartesia's inline tags (a `<spell>`'s content stays)."""
    return _tidy(text, _VOICE_TAG.sub("", text))


def strip_stage_directions(text: str) -> str:
    """`text` without bracketed asides or starred sentences, and untouched —
    spacing included — when it has none."""
    stripped = text
    for pattern in (_BRACKETED, _STARRED_SENTENCE, _CLOSE_AT_START, _OPEN_TO_END):
        stripped = pattern.sub("", stripped)
    return _tidy(text, stripped)


async def _keeping_voice_tags(text: str, text_type: str | TextType) -> str:
    return strip_stage_directions(text)


async def _stripping_voice_tags(text: str, text_type: str | TextType) -> str:
    return strip_voice_tags(strip_stage_directions(text))


def stage_direction_transform(*, voice_tags: bool = False) -> TextTransform:
    """`strip_stage_directions` as a pipecat TTS text transform, and
    `strip_voice_tags` unless the voice reads them (`voice_tags`). The same
    function every time, so two builds of one config compare equal."""
    return _keeping_voice_tags if voice_tags else _stripping_voice_tags
