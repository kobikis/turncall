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

`eager_eot_threshold` in `stt.extra` turns on the speculative reply: Flux's
EagerEndOfTurn starts the LLM while the turn is still open, and pipecat's LLM
holds what it writes until EndOfTurn confirms it — or drops it on TurnResumed.
A speculative tool call is withdrawn rather than run, so `end_call` cannot fire
on a guess. The cost is one inference per guess Flux takes back.

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


# Turn handling is chosen once, at build, from the primary STT (#204): Flux
# ends turns itself, the others need VAD + Smart Turn. After a failover switch
# across that line nothing would end the caller's turns but pipecat's 5s
# timeout, so a group is all Flux or none.
MIXED_FLUX_GROUP = (
    "stt.failover cannot mix Deepgram Flux (flux-*) with other STT models: "
    "Flux decides when the caller's turn ends and the others do not, so a "
    "switch between them would leave every turn waiting on a 5s timeout"
)


def mixes_flux(stt: Any) -> bool:
    """Whether an STT config and its failover entries straddle Flux."""
    return (
        len(
            {
                entry.provider == "deepgram" and is_flux(entry.model)
                for entry in (stt, *stt.failover)
            }
        )
        > 1
    )


def is_eager(settings: dict[str, Any]) -> bool:
    """Whether the agent asked Flux for its predicted end of turn."""
    return settings.get("eager_eot_threshold") is not None


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
        # Without it the threshold only makes Flux send predictions nobody
        # answers.
        enable_eager_end_of_turn=is_eager(settings),
        settings=resolved,
    )


def flux_turn_strategies(*, interruptions: bool, eager: bool = False) -> Any:
    """Turn start and end both Flux's: its StartOfTurn interrupts the agent
    (unless barge-in is off), its EndOfTurn ends the caller's turn — and with
    `eager`, its EagerEndOfTurn starts the reply that EndOfTurn releases.

    Passed explicitly rather than left to the service's recommendation: with no
    strategies given, the aggregator starts on pipecat's defaults — VAD plus a
    freshly loaded Smart Turn model — until the recommendation arrives.
    """
    from pipecat.turns.user_turn_strategies import (
        EagerUserTurnStrategies,
        ExternalUserTurnStrategies,
    )

    logger.info("Deepgram Flux owns end-of-turn; Smart Turn is not built")
    strategies = EagerUserTurnStrategies if eager else ExternalUserTurnStrategies
    return strategies(enable_interruptions=interruptions)
