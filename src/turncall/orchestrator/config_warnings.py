"""Advisory warnings derivable from an agent config alone (#148).

Two things an author needs told and neither of which may be a refusal.

A **respelling** that happens to look like IPA — `{"Metformin": "metformin"}` —
passes #140's notation validator, because every character in it is a legal IPA
symbol. It then reaches the provider as real markup: measured on Deepgram,
`met for min` synthesizes 25% more audio than the unhinted sentence, so the
voice says something else and the author hears about it from a caller. The check
is a heuristic, so it warns; `{"papa": "papa"}` is a correct transcription and a
validator that refused it would break a working agent to prevent a
mispronunciation.

Whether the voice can **read** the markup was already known at pipeline build
(#147) and said in a log line there — mid-call, to whoever reads server logs,
about a config written days earlier. It follows from `provider` + the resolved
`model` + `pronunciations`, so it belongs where the config is written.

Lives in `orchestrator/` because answering the second question means asking
pipecat — `format_pronunciation` and `ELEVENLABS_PHONEME_MODELS` — and this
package is where pipecat imports stay (CLAUDE.md). The API layer calls it from a
response validator the way `EvalScenarioResponse` calls `assertion_warnings`
(#95).
"""

from collections.abc import Collection
from typing import Any

from loguru import logger

from turncall.domain.models import AgentConfig

# Any word/IPA pair is enough to ask a provider whether it has a dialect at all;
# the answer does not depend on the word. Both are real so a future provider
# that validates its input still answers the question asked.
_PROBE_WORD = "Metformin"
_PROBE_IPA = "mɛtˈfɔɹmɪn"


def config_warnings(
    config: AgentConfig, kb_modes: Collection[str] = ()
) -> list[dict[str, Any]]:
    """Everything worth telling an author about this config, in `{code, message}`.

    `kb_modes` are the modes of the agent's linked knowledge bases, which live
    in their own table rather than the config.

    Never raises: the caller is a read of an agent, and losing the row to a
    failed advisory check would be a far worse bug than the one being reported.
    """
    try:
        return [
            warning
            for warning in (
                _respelling_warning(config),
                _readability_warning(config),
                knowledge_mode_warning(config, kb_modes),
                skills_on_s2s_warning(config),
            )
            if warning is not None
        ]
    except Exception as exc:  # pragma: no cover - defence, not a branch
        # Logged rather than swallowed (`coding-style.md`): silence here is how
        # a check that stopped running goes unnoticed for a release.
        logger.warning("agent_config_warnings_failed: {error}", error=str(exc))
        return []


def knowledge_mode_warning(
    config: AgentConfig, kb_modes: Collection[str]
) -> dict[str, Any] | None:
    """KB modes this agent's pipeline drops (#254), named with the provider.

    `auto` on any S2S agent: auto retrieval runs on each turn just before the
    LLM, and a speech-to-speech model has no such point. `prompt` and `tool`
    run everywhere. pipeline_builder drops what this names, so the build and
    the agent read cannot disagree.
    """
    if config.pipeline_mode != "s2s" or "auto" not in kb_modes:
        return None
    provider = config.s2s.provider
    return {
        "code": "knowledge_mode_unsupported",
        "message": (
            "A knowledge base linked in 'auto' mode is ignored on this S2S agent "
            f"({provider}): auto retrieval runs before each LLM turn, and a "
            "speech-to-speech model has no such point. Link it in 'tool' or "
            "'prompt' mode, or use pipeline_mode='cascade'."
        ),
        "modes": ["auto"],
        "provider": provider,
    }


def skills_on_s2s_warning(config: AgentConfig) -> dict[str, Any] | None:
    """Skills listed on an S2S agent (#428), which loads none.

    A load changes the prompt and tools mid-call, and a realtime session reads
    no such change after it starts on most providers. `skill_loader` skips the
    skills on exactly this rule and logs this message, so the build and the
    agent read cannot disagree.
    """
    if config.pipeline_mode != "s2s" or not config.skills:
        return None
    names = [ref.skill for ref in config.skills]
    return {
        "code": "skills_ignored_on_s2s",
        "message": (
            f"skills ({', '.join(names)}) are ignored on this S2S agent "
            f"({config.s2s.provider}): a realtime session takes no prompt or "
            "tool change mid-call, so calls run without them. Use "
            "pipeline_mode='cascade' to load skills."
        ),
        "skills": names,
        "provider": config.s2s.provider,
    }


def _respelling_warning(config: AgentConfig) -> dict[str, Any] | None:
    """Words whose "IPA" is the spelling typed back at us.

    Sharper than "contains no symbol outside ASCII lowercase", which fires on
    `si` (see) and `mi` (me) — real transcriptions of short English words.
    """
    suspicious = [
        word
        for word, ipa in config.tts.pronunciations.items()
        if _normalized(ipa) == _normalized(word)
    ]
    if not suspicious:
        return None
    return {
        "code": "pronunciation_looks_like_spelling",
        "message": (
            "tts.pronunciations: the pronunciation given for "
            f"{', '.join(suspicious)} is the spelling again, not IPA. It is "
            "accepted and sent to the voice as real markup, so the word is "
            "pronounced from those letters rather than mispronounced as before "
            "(e.g. 'met for min' for 'Metformin'). If it is genuinely the "
            "transcription, ignore this."
        ),
        "words": suspicious,
    }


def _normalized(value: str) -> str:
    return value.replace(" ", "").strip().lower()


def _readability_warning(config: AgentConfig) -> dict[str, Any] | None:
    """Say at create what #147 says at build: this voice will not read these."""
    if not config.tts.pronunciations:
        return None

    if config.pipeline_mode == "s2s":
        return {
            "code": "pronunciations_ignored_on_s2s",
            "message": (
                "tts.pronunciations is ignored on an S2S agent "
                "(pipeline_mode='s2s'): the model speaks directly and there is "
                "no TTS stage to apply the markup to."
            ),
            "words": sorted(config.tts.pronunciations),
        }

    provider = config.tts.provider
    service_cls = _tts_service_class(provider)
    if service_cls is None:
        return None

    model = _tts_that_runs(config)
    if service_cls.format_pronunciation(_PROBE_WORD, _PROBE_IPA) is None:
        reason = "has no pronunciation markup at all"
    elif not _model_reads_markup(provider, model, service_cls):
        reason = f"does not read pronunciation markup on model '{model}'"
    else:
        return None

    return {
        "code": "pronunciations_unreadable",
        "message": (
            f"tts.pronunciations: {provider} ({model}) {reason}, so these words "
            "are spoken as written: " + ", ".join(sorted(config.tts.pronunciations))
        ),
        "words": sorted(config.tts.pronunciations),
    }


def _model_reads_markup(provider: str, model: str | None, service_cls: Any) -> bool:
    """Whether this provider's *model* reads the dialect it has.

    Only ElevenLabs gates on the model: it reads SSML `<phoneme>` tags on the
    models in pipecat's `ELEVENLABS_PHONEME_MODELS` and nowhere else (#151
    measured that set and found it accurate — on `eleven_flash_v2_5` the tagged
    word is dropped). The set is pipecat's, never copied.
    """
    if provider != "elevenlabs":
        return True
    from pipecat.services.elevenlabs.tts import ELEVENLABS_PHONEME_MODELS

    return model in ELEVENLABS_PHONEME_MODELS


def _tts_service_class(provider: str) -> Any | None:
    """The pipecat class for a provider, imported only when it is the one used.

    None for a provider this does not know, which is a warning's business to
    stay quiet about — `TTSConfigSchema` is what refuses an unsupported one.
    """
    if provider == "deepgram":
        from pipecat.services.deepgram.tts import DeepgramTTSService

        return DeepgramTTSService
    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        return ElevenLabsTTSService
    if provider == "openai":
        from pipecat.services.openai.tts import OpenAITTSService

        return OpenAITTSService
    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        return CartesiaTTSService
    if provider == "xai":
        from pipecat.services.xai.tts import XAIHttpTTSService

        return XAIHttpTTSService
    return None


def _tts_that_runs(config: AgentConfig) -> str | None:
    """What the pipeline would actually run, not `config.tts.model`: the
    model, or the voice where that is what runs (Deepgram's voice is its
    model; xAI TTS has no model).

    That field's default is Deepgram's, so an ElevenLabs agent that named no
    model would otherwise be told its voice is `aura-2-helena-en` — a diagnostic
    pointing at a model nothing is running.
    """
    from turncall.orchestrator.pipeline_factory import _tts_runs

    return _tts_runs(config)
