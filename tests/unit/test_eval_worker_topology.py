"""The eval worker is a separate process, and the API is not it.

ADR-0004 is the reason and it is not stylistic: an eval run does full
STT+LLM+TTS at maximum speed, several at once, and that event-loop jitter
becomes dead air for a person on a live call. The rule is easy to break by
accident — someone "just running it inline" to avoid a queue — so it is
asserted structurally rather than left to review.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

API_MODULE = Path("src/turncall/api/v1/evals.py")

# Anything that stands up or drives a pipeline. The API may reference the
# runner's pure helpers (snapshot shaping); it may not execute a run.
FORBIDDEN_IN_API = (
    "execute_run",
    "run_iteration",
    "build_call_pipeline",
    "create_pipeline",
    "create_eval_transport",
    "EvalScriptSession",
    "EvalSimulationSession",
)


def test_the_api_router_never_executes_a_run() -> None:
    source = API_MODULE.read_text()
    for name in FORBIDDEN_IN_API:
        assert name not in source, (
            f"{API_MODULE} references {name!r} — eval pipelines must never run "
            "in the API process (ADR-0004: jitter here is dead air on a live call)"
        )


def test_the_api_router_imports_no_pipecat() -> None:
    """A pipecat import in the request path is the first step toward one."""
    tree = ast.parse(API_MODULE.read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not [m for m in imported if m.startswith("pipecat")]


def test_the_worker_has_its_own_entrypoint() -> None:
    """`turncall-eval-worker` is what makes the split real in a deployment."""
    pyproject = Path("pyproject.toml").read_text()
    assert 'turncall-eval-worker = "turncall.evals.worker:main"' in pyproject

    from turncall.evals import worker

    assert callable(worker.main)


def test_the_run_endpoint_accepts_rather_than_executes() -> None:
    """202, not 200: the caller is told the work is queued, not done."""
    source = API_MODULE.read_text()
    tree = ast.parse(source)
    statuses = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            for keyword in getattr(decorator, "keywords", []):
                if keyword.arg == "status_code":
                    statuses[node.name] = ast.literal_eval(keyword.value)
    assert statuses["create_eval_run"] == 202


def test_an_eval_context_disables_every_call_scoped_side_effect() -> None:
    """An eval has no `calls` row. Writing one, or dispatching call.ended for a
    conversation that was never a call, is the failure this guards."""
    from uuid import uuid4

    from turncall.orchestrator.pipeline_factory import CallContext

    common = {
        "call_id": uuid4(),
        "project_id": uuid4(),
        "agent_id": uuid4(),
        "call_sid": "x",
        "stream_sid": "x",
        "session_factory": object(),
    }
    assert CallContext(**common).is_eval is False
    assert CallContext(**common, eval_run_id=uuid4()).is_eval is True


def test_the_transcript_taps_stay_silent_for_an_eval() -> None:
    from unittest.mock import MagicMock, patch

    from turncall.orchestrator import observability

    context = MagicMock()
    context.is_eval = True

    async def _never() -> None:  # pragma: no cover - must not be scheduled
        raise AssertionError("an eval must not write call-scoped rows")

    with patch.object(observability, "_spawn") as spawn:
        observability._spawn_unless_eval(context, _never())
        spawn.assert_not_called()


@pytest.mark.asyncio
async def test_an_eval_writes_no_recording_and_no_recording_event() -> None:
    """Found by running an S2S probe: every eval iteration uploaded a WAV
    nothing pointed at and then raised a foreign-key error dispatching
    `recording.ready` for a call that does not exist. Caught and logged, so it
    was invisible — and the junk files accumulated.

    The processor itself must stay in the pipeline: only the transport is
    swapped in an eval, so dropping a stage would test a pipeline the caller
    never gets. Its side effects are what stop."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from uuid import uuid4

    from turncall.domain.enums import RecordingStatus
    from turncall.orchestrator import call_recorder

    context = MagicMock()
    context.is_eval = True
    context.call_id = uuid4()
    context.session_factory = AsyncMock()

    recorder = call_recorder.CallRecorder.__new__(call_recorder.CallRecorder)
    recorder._call_context = context

    with (
        patch.object(call_recorder, "create_storage_adapter") as storage,
        patch.object(call_recorder, "_pcm16_to_wav") as to_wav,
    ):
        await recorder._on_audio_data(MagicMock(), b"\x00\x01" * 100, 16000, 1)
        await recorder._set_status(RecordingStatus.IN_PROGRESS)

    storage.assert_not_called(), "an eval must not write a recording to storage"
    to_wav.assert_not_called()
    context.session_factory.assert_not_called()


def test_the_callers_synthesized_audio_is_cached_somewhere_durable() -> None:
    """#72: pipecat's caching TTS defaults to a directory under $HOME, which a
    container loses on every recreate — the caller's turns would then be
    re-synthesized on every run of every scenario. The worker points it at the
    configured path and creates it, so a bad path fails in the log rather than
    halfway through a turn."""
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch

    from turncall.domain.enums import EvalKind
    from turncall.evals import harness

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "eval-tts-cache"
        settings = SimpleNamespace(evals=SimpleNamespace(tts_cache_dir=str(target)))

        assert harness._tts_cache_dir(settings) == str(target)
        assert target.is_dir(), "created up front, not lazily mid-synthesis"

        captured = {}

        class _Params:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _Session:
            @staticmethod
            def from_scenario(parsed, url, params=None):
                return SimpleNamespace(parsed=parsed, url=url, params=params)

        with (
            patch("pipecat.evals.session.EvalSessionParams", _Params),
            patch("pipecat.evals.script_session.EvalScriptSession", _Session),
        ):
            harness._build_session(
                SimpleNamespace(),
                EvalKind.SCRIPTED,
                "ws://127.0.0.1:1",
                tts_cache_dir=str(target),
            )

    assert captured["cache_dir"] == str(target)
    # A fresh pipeline per iteration still means the bot is torn down with it.
    assert captured["stop_bot"] is True


def test_every_harness_caller_passes_the_tool_policy() -> None:
    """`run_iteration` takes `tool_mocks` with no default so a caller cannot
    forget it and get live tools (#71). Only live tests call it directly, and
    the hermetic suite never runs those — so the binding is checked here
    instead of discovered by a live run months later."""
    for path in Path("tests/live").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "run_iteration":
                continue
            passed = {kw.arg for kw in node.keywords}
            assert "tool_mocks" in passed, (
                f"{path}:{node.lineno} calls run_iteration without tool_mocks"
            )
