"""How a caller's turn is judged over: Smart Turn, or VAD's silence alone.

Two things pipecat 1.12 gets wrong for us, fixed here.

**Its default stop strategy is Smart Turn V3** (#269). An agent that turned
Smart Turn off and was handed pipecat's defaults still waited on the model,
after VAD's own `silence_timeout_ms` window. So every pipeline that has turn
strategies of its own passes an explicit stop strategy (`vad_turn_stop`).

**Each Smart Turn analyzer loads its own ONNX session** (#271): 36 MiB of
identical weights plus ONNX Runtime's per-session inference buffers — sharing
it took a call from ~123 MiB to ~45 (measured, `docs/guides/capacity.mdx`). The session holds only the model; what a call has heard — its audio
buffer, its speech state, its executor thread — lives on the analyzer, which
stays per call. ONNX Runtime's `run()` is safe to call from several threads at
once, and each analyzer runs the model on its own thread, so calls never queue
behind one another on a lock of ours.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.vad_analyzer import VAD_STOP_SECS
from pipecat.turns import user_turn_strategies as pipecat_turn_strategies
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

__all__ = [
    "VAD_STOP_SECS",
    "SharedSmartTurnAnalyzer",
    "s2s_vad_turn_strategies",
    "share_pipecats_default_model",
    "vad_turn_stop",
]


@cache
def _model_owner() -> LocalSmartTurnAnalyzerV3:
    """One real pipecat analyzer, built the first time a call needs Smart
    Turn: its bundled model, its session options, its env-driven flags. Never
    used for a call itself — only its session and settings are shared."""
    return LocalSmartTurnAnalyzerV3()


class SharedSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """pipecat's analyzer, minus the model load: the session is the process's.

    A subclass rather than a wrapper because pipecat's inference
    (`_predict_endpoint`) reads the *call's* sample rate off `self`; delegating
    to the owner would resample every call at the owner's rate. Skips
    `LocalSmartTurnAnalyzerV3.__init__` (whose only work is choosing the bundled
    model and loading it) and copies what it sets from `_model_owner`. A pipecat
    upgrade that gives that constructor more to set is caught by
    `test_smart_turn_shared_model.py`, which compares the two.
    """

    def __init__(self, **kwargs: Any) -> None:
        BaseSmartTurn.__init__(self, **kwargs)
        owner = _model_owner()
        self._log_data = owner._log_data
        self._session = owner._session


def _shared_default_stop_strategies() -> list:
    return [TurnAnalyzerUserTurnStopStrategy(turn_analyzer=SharedSmartTurnAnalyzer())]


def share_pipecats_default_model() -> None:
    """Make pipecat's *default* stop strategy use the shared session too.

    S2S on server VAD passes no turn strategies — any it passed would override
    the ones an OpenAI realtime service recommends — so pipecat builds its
    default there, and the default is a fresh `LocalSmartTurnAnalyzerV3`: one
    model load per call. `UserTurnStrategies` looks the factory up in its
    module at construction, so replacing it there is enough. Idempotent.
    """
    pipecat_turn_strategies.default_user_turn_stop_strategies = (
        _shared_default_stop_strategies
    )


def vad_turn_stop(extra_wait_secs: float = 0.0) -> SpeechTimeoutUserTurnStopStrategy:
    """End the turn when VAD reports silence, waiting `extra_wait_secs` more.

    VAD's own `stop_secs` window already is the pause that ends a turn, so the
    extra wait is normally zero: pipecat's 0.6s default would stack on top of
    it the way Smart Turn did.
    """
    return SpeechTimeoutUserTurnStopStrategy(
        user_speech_timeout=max(0.0, extra_wait_secs)
    )


def s2s_vad_turn_strategies() -> UserTurnStrategies:
    """S2S on `pipecat_vad`: VAD's `silence_timeout_ms` window ends the turn.

    S2S has no Smart Turn. Already in the shape realtime mode wants — no
    transcription start, no waiting for a transcript — so pipecat has nothing to
    rewrite. Only on `pipecat_vad`: on server VAD an OpenAI realtime service
    recommends its own external strategies, which any set passed here would
    override.
    """
    return UserTurnStrategies(
        start=[VADUserTurnStartStrategy()],
        stop=[
            SpeechTimeoutUserTurnStopStrategy(
                user_speech_timeout=0.0, wait_for_transcript=False
            )
        ],
    )


def silero_vad(*, sample_rate: int, stop_secs: float, pipecat_settings: Any) -> Any:
    """Silero with the deployment's `PIPECAT_VAD_CONFIDENCE_THRESHOLD`: every VAD
    a call builds comes from here, so none can quietly drop the setting."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams

    confidence = getattr(pipecat_settings, "vad_confidence_threshold", None)
    params = VADParams(
        stop_secs=stop_secs,
        **({"confidence": confidence} if confidence is not None else {}),
    )
    return SileroVADAnalyzer(sample_rate=sample_rate, params=params)
