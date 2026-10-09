"""Deepgram Flux: the STT that decides when the caller's turn is over (#204).

Flux is a Deepgram model family (`flux-general-en`, `flux-general-multi`) on its
own endpoint (`/v2/listen`), so pipecat serves it with its own service. Naming a
`flux-*` model on the `deepgram` provider is the whole opt-in: same account,
same key, no new config field.

Flux owns end-of-turn. It reports StartOfTurn and EndOfTurn itself, as proposals
the user aggregator resolves (pipecat's `ExternalUserTurnStrategies`), so the
turn ends when Flux says it did. Smart Turn is not built for such an agent:
its stop strategy would hold every turn for a verdict of its own after Flux had
already given one — the same serial stacking that once put end-of-turn 1.8s
after the caller stopped, when VAD and Smart Turn each waited in turn.
`silence_timeout_ms` is not read either; Flux's own `eot_threshold` /
`eot_timeout_ms` are the knobs, through `stt.extra`.

VAD stays, at pipecat's 0.2s, and decides nothing. It is what pipecat's latency
observer measures from (the caller's actual silence), so without it a Flux call
would record no `metrics.turn` at all; it also keeps the user-speaking signal
the idle guard reads.
"""

from typing import Any

from loguru import logger

_FLUX_PREFIX = "flux"


def is_flux(model: str) -> bool:
    """Whether a resolved Deepgram model name is a Flux model."""
    return str(model).startswith(_FLUX_PREFIX)


def create_flux_stt(
    *,
    api_key: str,
    settings: dict[str, Any],
    sample_rate: int,
    interruptions: bool,
) -> Any:
    """The Flux service, with `settings` mapped onto its declared fields.

    Flux builds its connection URL from declared settings only — its `extra`
    is never sent — so a key in `stt.extra` naming one of them
    (`eot_threshold`, `eot_timeout_ms`, `eager_eot_threshold`, `min_confidence`,
    `language_hints`, ...) is set as that field, and any other is reported
    rather than silently lost.
    """
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

    resolved = DeepgramFluxSTTService.Settings.from_mapping(settings)
    if resolved.extra:
        logger.warning(
            "Deepgram Flux ignores stt.extra keys it does not declare: {keys}",
            keys=sorted(resolved.extra),
        )
    return DeepgramFluxSTTService(
        api_key=api_key,
        sample_rate=sample_rate,
        # What Flux recommends to the aggregator. TurnCall passes its own
        # strategies (below), so this only keeps the two in agreement — a
        # disagreement is what pipecat warns about.
        should_interrupt=interruptions,
        settings=resolved,
    )


def flux_turn_strategies(*, interruptions: bool) -> Any:
    """Turn start and end both Flux's: its StartOfTurn interrupts the agent
    (unless barge-in is off), its EndOfTurn ends the caller's turn.

    Passed explicitly rather than left to the service's recommendation: with no
    strategies given, the aggregator starts on pipecat's defaults — VAD plus a
    freshly loaded Smart Turn model — until the recommendation arrives.
    """
    from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

    logger.info("Deepgram Flux owns end-of-turn; Smart Turn is not built")
    return ExternalUserTurnStrategies(enable_interruptions=interruptions)
