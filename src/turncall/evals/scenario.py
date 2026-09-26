"""Parsing and validating a stored scenario definition.

`definition` is pipecat's own scenario mapping, stored verbatim as JSONB.
Validating it means round-tripping it through pipecat's parser and surfacing
pipecat's own error — the schema belongs to pipecat and moves between majors,
so tracking it in our own models would be a standing migration debt.

That round-trip goes through two of pipecat's **private** functions, because
there is still no public mapping-level parser as of 1.12: `EvalScenarioFile.load`
takes a path, and a stored scenario has no file. See `_parsers` and #100.

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

from loguru import logger

from turncall.domain.enums import EvalKind, EvalModality

# The pipecat scenario schema a definition is validated against, recorded on
# every scenario so a pipecat major upgrade can find the rows that predate it.
#
# Rows written before a bump keep the version they were validated against —
# that is the column's whole job, and rewriting it would erase the only record
# of which parser accepted them. 1.12 changed no field of either scenario
# dataclass (tests/unit/test_pipecat_parser_contract.py), so a 1.11 row is
# still valid; the version says when it was checked, not whether it works.
SCHEMA_VERSION = "pipecat-1.12"

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


def _parsers() -> dict[EvalKind, Any]:
    """Pipecat's parsers for the two scenario kinds, by kind.

    Both are underscore-private and carry no compatibility promise (#100). They
    are used anyway because the public entry points in pipecat 1.11 are
    file-based — `EvalScenarioFile.load(path)` reads YAML off disk, and the
    mapping-level `_scenario_from_mapping` beneath it is private too. A stored
    definition has no file, and writing one per validation to reach a public
    API would add I/O to every create and every queued run, and a YAML
    round-trip to a mapping that is already parsed.

    What this costs is a rename in a pipecat minor taking out **all** scenario
    validation at once: creating, updating, and `_parse_for_run` on every run in
    the queue. `tests/unit/test_pipecat_parser_contract.py` is what turns that
    into a red build with an obvious cause instead, and the error below is what
    the worker's log says if one ever ships anyway.
    """
    from importlib.metadata import version

    try:
        from pipecat.evals.script import _parse_script
        from pipecat.evals.simulation import _parse_simulation
    except ImportError as exc:  # pragma: no cover - the upgrade this warns about
        try:
            installed = version("pipecat-ai")
        except Exception:
            installed = "unknown"
        raise ScenarioError(
            f"pipecat {installed} does not expose the scenario parsers TurnCall "
            f"validates against ({SCHEMA_VERSION}): {exc}. This is a pipecat "
            "upgrade, not a problem with this scenario — see #100."
        ) from exc
    return {EvalKind.SCRIPTED: _parse_script, EvalKind.SIMULATION: _parse_simulation}


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
    parser_for = _parsers()
    kind = kind_of(definition)
    data = dict(definition)
    if name is not None:
        data["name"] = name

    parser = parser_for[kind]
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


# What makes an expectation an assertion rather than a shape check. A bare
# `{"event": "llm_response"}` asserts only that *something* arrived: after a
# provider 404 pipecat still emits an empty `llm_response`, so such a scenario
# reports `passed` against an agent whose LLM returns nothing — which is the
# exact class of regression (#63/#64/#65) the feature was built to catch.
_ASSERTING_FIELDS = (
    "text_contains",
    "text_excludes",
    "eval",
    "calls",
    "marker",
    "markers",
    "marker_first",
    "text_after",
)
# `matches` is deliberately absent: pipecat 1.11's expectation has no such
# field and its parser drops the key, so an expectation whose only check is
# `matches:` really does assert nothing and *should* be warned about. CLAUDE.md
# used to name it in the vocabulary; that was the documentation being wrong,
# and this check is what caught it.


def _asserts_something(expectation: Any) -> bool:
    """Whether one expectation can fail on anything but a missing event.

    Presence, not truthiness: `markers: 0` ("no markers arrived") and
    `text_after: false` ("the marker is not followed by text") are assertions
    whose values are falsy, and reading those as unset reported a scenario that
    can fail as one that cannot — the exact reverse of the feature.

    `absent: True` counts too: asserting an event never arrives is a claim
    about behaviour, and pipecat forbids combining it with the content checks.
    """
    if getattr(expectation, "absent", False):
        return True
    return any(
        getattr(expectation, field, None) is not None for field in _ASSERTING_FIELDS
    )


def _cannot_fail(message: str, weak: list[str]) -> dict[str, Any]:
    """The loud case: nothing in this scenario can report a regression."""
    return {"code": "scenario_cannot_fail", "message": message, "expectations": weak}


def assertion_warnings(
    scenario: Any, *, name: str | None = None
) -> list[dict[str, Any]]:
    """Say so when a scripted scenario cannot fail for the right reason (#95).

    Advisory, never fatal — `_warn_unmatched_mocks` sets that precedent, and
    the asymmetric cases are real: a turn may legitimately assert only that a
    function call happened. What is never intended is a whole scenario of bare
    events, which parses cleanly, stores, runs, and stays green through every
    broken provider.

    A simulation is exempt: its judge rules on `success` over the whole
    conversation, so there is always something to fail.

    Takes a parsed scenario or the raw definition, since the API has the one
    and the runner has the other.
    """
    turns = getattr(_parsed(scenario, name), "turns", None)
    if not turns:
        return []
    return _warning_for(*_weak_expectations(turns))


def _parsed(scenario: Any, name: str | None) -> Any:
    """The scenario as pipecat's object, parsing a raw definition if needed."""
    if not isinstance(scenario, dict):
        return scenario
    try:
        # Pipecat requires a `name:` key at all; the row's name is
        # authoritative and a placeholder stands in for a draft that has none,
        # so a missing name never reads as "nothing to warn about".
        return parse(scenario, name=name or "<scenario>")
    except ScenarioError as exc:
        # Shape is the validator's business and a definition that cannot parse
        # has a louder problem — but not silently: `coding-style.md` is right
        # that a swallowed error nobody can see is how this gets misdiagnosed.
        logger.debug("assertion_check_skipped", scenario=name, error=str(exc))
        return None


def _weak_expectations(turns: list[Any]) -> tuple[int, list[str]]:
    """How many expectations there are, and which assert no content."""
    total = 0
    weak: list[str] = []
    for index, turn in enumerate(turns, start=1):
        for expectation in getattr(turn, "expect", None) or []:
            total += 1
            if not _asserts_something(expectation):
                weak.append(f"turn {index}: {getattr(expectation, 'event', '?')}")
    return total, weak


def _warning_for(total: int, weak: list[str]) -> list[dict[str, Any]]:
    """What a turn count and its weak expectations add up to, if anything."""
    add = " Add text_contains, text_excludes, eval: or a function_call with its args."
    if total == 0:
        return [_cannot_fail("this scenario's turns carry no expectations." + add, [])]
    if not weak:
        return []
    if len(weak) == total:
        return [
            _cannot_fail(
                "no expectation in this scenario asserts any content, so it passes "
                "whenever the events merely arrive — including when the LLM is "
                "returning nothing at all (a provider that 404s still emits an "
                "empty llm_response)." + add,
                weak,
            )
        ]
    return [
        {
            "code": "content_free_expectations",
            "message": (
                f"{len(weak)} of {total} expectations assert only that an event "
                "arrived: " + "; ".join(weak)
            ),
            "expectations": weak,
        }
    ]


# Every place pipecat will read a `factory` from, and therefore every place a
# stored definition could smuggle one in. `factory` is a dotted path pipecat
# hands to `importlib.import_module`, so a definition carrying one turns a
# scenario create into "import this module on the eval worker" (#118).
_FACTORY_PATHS = (
    ("judge", "eval"),
    ("judge", "transcription"),
    ("simulator",),
    ("user", "speech"),
)


def reject_factories(definition: dict[str, Any]) -> None:
    """Refuse a definition that names a `factory` anywhere pipecat imports one.

    TurnCall ships the factories (`evals.judges.PROVIDERS`) and a request names
    a provider; a dotted path from a caller is never imported. Checked at the
    API boundary, where the definition arrives, rather than at run time, where
    the worker would already have the module loaded.

    Raises:
        ScenarioError: A `factory` key is present.
    """
    for path in _FACTORY_PATHS:
        node: Any = definition
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and "factory" in node:
            where = ".".join(path)
            raise ScenarioError(
                f"{where}.factory is not accepted: it is a dotted path this "
                "service would import. Name a `provider` on the scenario's "
                f"judge/simulator block instead (one of: "
                f"{', '.join(sorted(_provider_names()))})."
            )


def _provider_names() -> list[str]:
    from turncall.evals.judges import PROVIDERS

    return list(PROVIDERS)


def compile_model_block(
    block: dict[str, Any] | None, *, role: str = "judge"
) -> dict[str, Any] | None:
    """One typed `{provider, model, temperature, endpoint}` as pipecat's mapping.

    Temperature rides in `extra`, which pipecat forwards as top-level request
    parameters — there is no first-class field for it. Anthropic gets none at
    all: current Claude models reject the parameter outright, and the rule
    belongs to the model rather than to whoever configured it, exactly as on
    the call path.
    """
    if not block:
        return None
    from turncall.evals.judges import JUDGE_PROVIDERS, PROVIDERS

    # A judge may be anything that answers a question; a simulator has to be an
    # LLM service, because the persona is linked into a pipeline to speak.
    providers = JUDGE_PROVIDERS if role == "judge" else PROVIDERS

    provider = str(block.get("provider") or "ollama")
    if provider not in providers:
        raise ScenarioError(f"unknown {role} provider {provider!r}")

    compiled: dict[str, Any] = {"factory": providers[provider]}
    for key in ("model", "endpoint"):
        if block.get(key):
            compiled[key] = block[key]

    temperature = block.get("temperature")
    if temperature is not None and _takes_temperature(provider, compiled.get("model")):
        compiled["extra"] = {"temperature": temperature}
    return compiled


# OpenAI's reasoning families reject `temperature` the way Claude does; the
# prefixes are the ones `llm.reasoning_effort` already documents.
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _takes_temperature(provider: str, model: str | None) -> bool:
    """Whether this model accepts a temperature at all.

    Anthropic never does — current Claude models answer `400 temperature is
    deprecated for this model`, which is the bug that made the service
    unusable on the call path. An OpenAI reasoning model rejects it too. The
    rule travels with the model rather than the caller, so a scenario that
    names one is not punished for a parameter it never asked about.
    """
    if provider == "anthropic":
        return False
    name = (model or "").lower()
    return not any(name.startswith(prefix) for prefix in _REASONING_PREFIXES)


def with_models(
    definition: dict[str, Any],
    *,
    judge: dict[str, Any] | None = None,
    simulator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The definition with the scenario's judge and persona LLMs applied.

    A raw block already inside the definition **wins**. It was there first, and
    a stored scenario whose verdicts were decided by the judge it names must
    not start being decided by a different one because a typed field appeared
    beside it.
    """
    merged = copy.deepcopy(definition)

    compiled_judge = compile_model_block(judge)
    if compiled_judge:
        block = merged.setdefault("judge", {})
        if isinstance(block, dict) and not block.get("eval"):
            block["eval"] = compiled_judge

    compiled_persona = compile_model_block(simulator, role="simulator")
    if compiled_persona and not merged.get("simulator"):
        merged["simulator"] = compiled_persona
    return merged
