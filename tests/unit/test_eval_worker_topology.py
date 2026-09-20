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
