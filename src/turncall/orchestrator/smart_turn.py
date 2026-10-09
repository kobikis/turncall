"""Smart Turn V3 with one ONNX session per process (#271).

pipecat's `LocalSmartTurnAnalyzerV3` loads its own `InferenceSession` in its
constructor, and a call builds one analyzer — so every call held its own copy
of the same weights: about 36 MiB of the ~115 MiB a call costs (measured,
`docs/guides/capacity.mdx`). The session holds only the model; what a call has
heard (its audio buffer, its speech state, its executor thread) lives on the
analyzer, which stays per call. ONNX Runtime's `run()` is safe to call from
several threads at once, and each analyzer runs the model on its own thread,
so calls never queue behind one another on a lock of ours.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3


@cache
def _model_owner() -> LocalSmartTurnAnalyzerV3:
    """One real pipecat analyzer, built the first time a call needs Smart
    Turn: its bundled model, its session options, its env-driven flags. Never
    used for a call itself — only its session and settings are shared."""
    return LocalSmartTurnAnalyzerV3()


class SharedSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """pipecat's analyzer, minus the model load: the session is the process's.

    Skips `LocalSmartTurnAnalyzerV3.__init__` (whose only work is choosing the
    bundled model and loading it) and copies what it sets from `_model_owner`.
    A pipecat upgrade that gives that constructor more to set is caught by
    `test_smart_turn_shared_model.py`, which compares the two.
    """

    def __init__(self, **kwargs: Any) -> None:
        BaseSmartTurn.__init__(self, **kwargs)
        owner = _model_owner()
        self._log_data = owner._log_data
        self._session = owner._session
