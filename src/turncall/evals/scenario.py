"""Parsing and validating a stored scenario definition.

`definition` is pipecat's own scenario mapping, stored verbatim as JSONB.
Validating it means round-tripping it through pipecat's parser and surfacing
pipecat's own error — the schema belongs to pipecat and moves between majors,
so tracking it in our own models would be a standing migration debt.

`_parse_script` / `_parse_simulation` take `(mapping, path)`; the path is only
used in error messages and to resolve a turn's relative `audio:`/`image:`
paths. A stored scenario has no file, so a label stands in — and an audio path
is therefore unusable from a stored definition, which is fine until a slice
needs it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from turncall.domain.enums import EvalKind, EvalModality

# The pipecat scenario schema a definition is validated against, recorded on
# every scenario so a pipecat major upgrade can find the rows that predate it.
SCHEMA_VERSION = "pipecat-1.11"

# Stands in for the file path pipecat's parsers name in their errors.
_STORED = Path("<stored scenario>")


class ScenarioError(ValueError):
    """A definition pipecat's parser rejected, or one of neither kind."""


def kind_of(definition: dict[str, Any]) -> EvalKind:
    """Which kind of scenario a mapping declares.

    Computed eagerly at the API boundary and stored as a column, so every
    reader switches on an explicit value instead of sniffing nullability.
    Exactly one of `turns:` (scripted) and `persona:` (a simulation).
    """
    has_turns = "turns" in definition
    has_persona = "persona" in definition
    if has_turns and has_persona:
        raise ScenarioError(
            "a scenario is scripted ('turns') or a simulation ('persona'), not both"
        )
    if has_turns:
        return EvalKind.SCRIPTED
    if has_persona:
        return EvalKind.SIMULATION
    raise ScenarioError(
        "a scenario needs 'turns' (scripted) or 'persona' (a simulation)"
    )


# The voice the caller's turns are synthesized in. Pipecat requires a
# `user.speech:` block (service + voice) as soon as a turn has to be spoken and
# names no `audio:` file of its own — there is no implicit default — so a run
# asking for audio brings one. Kokoro is local: no key, no per-turn cost, and
# the same utterance is synthesized once and cached across runs.
DEFAULT_USER_SPEECH = {"service": "kokoro", "voice": "af_heart"}

# The STT that transcribes the bot's audio for the judge in audio modality.
# Pipecat *requires* a `judge.transcription:` block there and raises without
# one, so a run asking for audio has to bring a default or every audio scenario
# is unrunnable. Moonshine is pipecat's own default and runs locally — no key,
# no per-run cost, and no transcript leaving the box, which is the same
# property the local judge has (see ADR-0018 on the Ollama default).
DEFAULT_BOT_TRANSCRIPTION = {"service": "moonshine"}


def with_modality(definition: dict[str, Any], modality: EvalModality) -> dict[str, Any]:
    """The definition with the run's modality applied as the default.

    Pipecat splits `user.modality` and `judge.modality` independently — four
    combinations. A run exposes one knob, so it sets both; a scenario that
    names either one keeps its own, which is how the useful asymmetric pair
    (`user: audio, judge: text` — exercises STT, skips TTS) stays reachable.

    Audio also needs services the text path never builds: a TTS to speak the
    caller's turns (pipecat defaults it to local Kokoro) and an STT to
    transcribe the bot's (no default at all — pipecat raises). The missing one
    is filled here rather than demanded of every scenario, because `modality:
    audio` on the run is the whole interface this exposes.
    """
    merged = copy.deepcopy(definition)
    for block in ("user", "judge"):
        existing = merged.get(block)
        if existing is not None and not isinstance(existing, dict):
            # Let pipecat's parser produce the error for a malformed block.
            continue
        merged[block] = {"modality": modality.value, **(existing or {})}

    user, judge = merged["user"], merged["judge"]
    if (
        isinstance(user, dict)
        and user.get("modality") == EvalModality.AUDIO.value
        and user.get("speech") is None
    ):
        user["speech"] = dict(DEFAULT_USER_SPEECH)
    if (
        isinstance(judge, dict)
        and judge.get("modality") == EvalModality.AUDIO.value
        and judge.get("transcription") is None
    ):
        judge["transcription"] = dict(DEFAULT_BOT_TRANSCRIPTION)
    return merged


def parse(definition: dict[str, Any], *, name: str | None = None) -> Any:
    """Parse a definition into pipecat's scenario dataclass.

    Args:
        definition: The stored mapping, already modality-resolved if needed.
        name: Overrides the mapping's own `name:` — the row's name is
            authoritative, and pipecat requires the key to be present at all.

    Returns:
        An `EvalScriptScenario` or `EvalSimulationScenario`.

    Raises:
        ScenarioError: The mapping is of neither kind, or pipecat rejected it.
    """
    from pipecat.evals.script import _parse_script
    from pipecat.evals.simulation import _parse_simulation

    kind = kind_of(definition)
    data = dict(definition)
    if name is not None:
        data["name"] = name

    parser = _parse_script if kind is EvalKind.SCRIPTED else _parse_simulation
    try:
        return parser(data, _STORED)
    except ScenarioError:
        raise
    except Exception as exc:
        # Pipecat's own message names the offending field; it is more useful to
        # the author than anything we could paraphrase.
        raise ScenarioError(str(exc)) from exc


def validate(definition: dict[str, Any], *, name: str | None = None) -> EvalKind:
    """Validate a definition at the API boundary and return its kind.

    Round-tripping through the parser is the validation: what a run will later
    build is exactly what is checked here, so a definition that is stored is
    one that can run.
    """
    parse(definition, name=name)
    return kind_of(definition)
