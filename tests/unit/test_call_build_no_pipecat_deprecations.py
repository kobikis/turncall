"""A cascade call's build uses nothing pipecat has deprecated (pipecat 1.14).

The eval path has its own pins (`test_eval_no_pipecat_deprecations.py`); this
is the call's half: every TTS provider and the tools schema, the two places
1.14's deprecations reached (`text_filters`, `pipecat.adapters.schemas`). In a
child process, because a deprecated module warns once, on its first import, and
in this one an earlier test may already have imported it. Matched on pipecat's
own template, so the third-party warnings its imports raise are not counted —
nor `PIPECATS_OWN`, deprecations pipecat sets off inside itself.
"""

import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

# pipecat 1.14's `tts_service.py` subclasses its own deprecated
# `AudioContextTTSService` at import: nothing TurnCall calls.
PIPECATS_OWN = ("`AudioContextTTSService`",)

_SCRIPT = textwrap.dedent(
    """
    import warnings
    warnings.simplefilter("always")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from tests.conftest import creds
        from turncall.domain.models import AgentConfig, TTSConfig
        from turncall.orchestrator import pipeline_factory

        for provider in ("deepgram", "elevenlabs", "openai", "cartesia", "xai"):
            config = AgentConfig(tts=TTSConfig(provider=provider))
            pipeline_factory._create_tts_service(config, creds("x"))
        tool = {
            "name": "lookup",
            "description": "Look a customer up.",
            "parameters_schema": {"type": "object", "properties": {}},
            "webhook_url": "https://example.com/tool",
        }
        schema = pipeline_factory._build_tools_schema(
            AgentConfig.model_validate({"tools": [tool]})
        )
        assert schema is not None
    from pipecat.utils.deprecation import DEPRECATION_MESSAGE_RE
    for w in caught:
        if DEPRECATION_MESSAGE_RE.search(str(w.message)):
            print("DEPRECATED:", w.message)
    print("built")
    """
)


def test_building_a_cascade_call_warns_of_no_pipecat_deprecation() -> None:
    done = subprocess.run(  # noqa: S603 - our own interpreter and code
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1", "CARTESIA_API_KEY": "x"},
    )
    assert "built" in done.stdout, done.stderr
    ours = [
        line
        for line in done.stdout.splitlines()
        if line.startswith("DEPRECATED:") and not any(n in line for n in PIPECATS_OWN)
    ]
    assert ours == [], done.stdout
