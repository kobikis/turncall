"""IPA validation for the per-agent pronunciation dictionary (#140).

Pipecat's own IPA utilities normalize and tokenize but never reject: they read
``met-FOR-min`` as ten phones and ``xyz123`` as six, then hand the nonsense to
the provider, which does nothing with it. ASCII respelling is what an author
types first, so the rejection is TurnCall's own and belongs at the API
boundary — pipecat reports an unusable pronunciation exactly once, when the
transform is built, which on a live call is at connect with a caller already
listening.

What is checked is notation: every character has to be an IPA one. Whether the
sequence is *pronounceable* is deliberately not checked — that is the author's
business.
"""

from collections.abc import Mapping

# IPA lives in these blocks. Stress (ˈ ˌ), length (ː), tone letters and the
# superscript modifiers are Spacing Modifier Letters; the tie bar and
# nasalization are combining marks.
_IPA_RANGES = (
    (0x0250, 0x02AF),  # IPA Extensions
    (0x02B0, 0x02FF),  # Spacing Modifier Letters
    (0x0300, 0x036F),  # Combining Diacritical Marks
    (0x1D00, 0x1DBF),  # Phonetic Extensions + Supplement
    (0x1DC0, 0x1DFF),  # Combining Diacritical Marks Supplement
)

# IPA symbols that predate those blocks, plus the prosody marks. Greek theta,
# beta and chi are IPA letters; the rest of the Greek alphabet is not. Lowercase
# ASCII is IPA; uppercase and digits are not, which is what makes respelled
# ASCII ("met-FOR-min") a rejection rather than a silent no-op.
_IPA_EXTRAS = "abcdefghijklmnopqrstuvwxyzæçðøħŋœǀǁǂǃβθχ |‖‿↗↘"

_IPA_CHARS = frozenset(_IPA_EXTRAS).union(
    chr(code) for lo, hi in _IPA_RANGES for code in range(lo, hi + 1)
)


def normalize_pronunciations(pronunciations: Mapping[str, str]) -> dict[str, str]:
    """Clean a word→IPA map, or raise naming the word that is not IPA.

    Blank words and blank values are dropped, so an empty map is exactly
    equivalent to the field being absent. Values are stored in pipecat's own
    canonical notation — surrounding ``/…/`` or ``[…]`` gone, composed, ``ʧ``
    written ``tʃ``, ``'`` written ``ˈ`` — so two spellings of one transcription
    are one stored config, and the stored form is what the transform will read.

    Raises:
        ValueError: A value contains characters that are not IPA.
    """
    # Pipecat's normalizer, not a copy of it: the stored canonical form has to
    # be the one the transform consumes, and a second implementation would
    # drift from it silently — two spellings of one transcription would become
    # two stored configs again. A deliberate exception to "pipecat imports live
    # in orchestrator/" (CLAUDE.md), kept function-local so importing this
    # module costs nothing, and narrow: one pure notation function, no frames,
    # no services.
    from pipecat.utils.text.phonemes import normalize_ipa

    cleaned: dict[str, str] = {}
    for raw_word, raw_ipa in pronunciations.items():
        word = raw_word.strip()
        if not word:
            continue
        ipa = normalize_ipa(raw_ipa)
        if not ipa:
            continue
        bad = "".join(sorted({c for c in ipa if c not in _IPA_CHARS}))
        if bad:
            raise ValueError(
                f"pronunciation for {word!r} is not IPA: {raw_ipa!r} contains "
                f"{bad!r}. Use IPA (Metformin -> 'mɛtˈfɔɹmɪn'), not respelled "
                f"ASCII ('met-FOR-min') — every TTS provider ignores that."
            )
        cleaned[word] = ipa
    return cleaned
