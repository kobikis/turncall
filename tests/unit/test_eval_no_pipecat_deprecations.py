"""The eval path uses nothing pipecat has deprecated (#375).

pipecat 1.13 deprecated the three things the eval worker was built on, all
removed in 2.0.0: `EvalTransport` (the server the bot hosted), the
`pipecat.classifiers.jev` module path, and `EvalSessionParams`'s
`stop_bot`/`trigger_disconnect` flags. Each one would make the 2.0 bump an
eval-transport rewrite too, so each is pinned here by the warning pipecat
itself emits for it — its canonical template, `DEPRECATION_MESSAGE_RE`, which
every pipecat deprecation follows. Third-party warnings pipecat's imports raise
(`audioop`, `websockets.legacy`) are not pipecat's and not ours to fix.

A whole iteration under the same filter is in
`tests/integration/test_speech_failover_eval.py`.
"""

import subprocess
import sys
import textwrap
import warnings
from unittest.mock import MagicMock

import pytest
from pipecat.utils.deprecation import DEPRECATION_MESSAGE_RE

from turncall.domain.enums import EvalKind
from turncall.evals import harness, scenario

pytestmark = pytest.mark.unit


def pipecat_deprecations(caught: list[warnings.WarningMessage]) -> list[str]:
    return [
        str(w.message) for w in caught if DEPRECATION_MESSAGE_RE.search(str(w.message))
    ]


def test_the_template_is_what_pipecat_warns_with() -> None:
    """Otherwise every assertion below passes against a renamed template."""
    from pipecat.evals.session import EvalSessionParams

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalSessionParams(stop_bot=True)
    assert pipecat_deprecations(caught), [str(w.message) for w in caught]


def test_the_harness_session_sets_no_deprecated_flag() -> None:
    parsed = scenario.parse({"turns": [{"user": "hi", "expect": []}]}, name="x")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        session = harness._build_session(
            parsed, EvalKind.SCRIPTED, "ws://127.0.0.1:1", tts_cache_dir=None
        )
    assert session is not None
    assert pipecat_deprecations(caught) == []


@pytest.mark.parametrize("audio", [False, True])
def test_the_eval_transport_is_not_the_deprecated_one(audio: bool) -> None:
    from pipecat.evals.serializer import EvalConnectionFlags

    from turncall.orchestrator.transport_factory import create_eval_transport

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        transport = create_eval_transport(
            MagicMock(), EvalConnectionFlags(skip_tts=not audio), audio=audio
        )
    assert pipecat_deprecations(caught) == []
    assert transport._params.audio_out_enabled is audio


def test_building_the_jev_judge_imports_no_deprecated_module() -> None:
    """In a child process: the old module warns once, on first import, so in
    this one a test that imported it earlier would hide it. Passes with or
    without the `evals-jev` extra installed: the import comes first."""
    script = textwrap.dedent(
        """
        import warnings
        warnings.simplefilter("always")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from turncall.evals import judges
            judges._platform_key = lambda attr: "ts-test"
            try:
                judges.jev({})
            except judges.JudgeUnavailable:
                # No h2 (the extra is not installed): the classifier's module
                # was still imported, which is where the warning would be.
                pass
        from pipecat.utils.deprecation import DEPRECATION_MESSAGE_RE
        for w in caught:
            if DEPRECATION_MESSAGE_RE.search(str(w.message)):
                print("DEPRECATED:", w.message)
        print("built")
        """
    )
    done = subprocess.run(  # noqa: S603 - our own interpreter and code
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=_child_env(),
    )
    assert "built" in done.stdout, done.stderr
    assert "DEPRECATED:" not in done.stdout, done.stdout


def _child_env() -> dict[str, str]:
    import os

    return {**os.environ, "PYTHON_DOTENV_DISABLED": "1"}
