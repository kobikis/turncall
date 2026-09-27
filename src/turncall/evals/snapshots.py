"""The snapshots a run records before its first iteration (ADR-0018).

A verdict is uninterpretable without them, and all three can change after the
run: the agent config that actually ran, the scenario as it stood, and the
harness — which judge decided, which model explained, which voice and STT the
audio path used, and which code executed it.

Pure functions of a parsed scenario and a stored row: no session, no settings,
no pipeline. `runner.py` keeps the orchestration that writes them.
"""

from __future__ import annotations

from typing import Any

from turncall.evals import scenario as scenario_mod
from turncall.evals.build import worker_build


def harness_config(parsed: Any = None) -> dict[str, Any]:
    """What the harness side of the run was, for the run's third snapshot.

    The judge decides the verdict, so a result cannot be compared across time
    without knowing which model answered — a silent provider-side model update
    moves the whole baseline, and pipecat's own default (ollama/gemma) is not
    the one most people think they are running. Read off the parsed scenario
    rather than passed in, because `judge.eval:` is the scenario's to set and
    pipecat fills the default when it does not.

    Args:
        parsed: The pipecat scenario about to run, or None before one exists.
    """
    from importlib.metadata import version

    try:
        pipecat_version = version("pipecat-ai")
    except Exception:  # pragma: no cover - packaging metadata is always there
        pipecat_version = "unknown"
    judge = getattr(parsed, "judge", None) or {}
    return {
        "pipecat_version": pipecat_version,
        # Pipecat fills `judge.eval:` with its ollama default whether or not a
        # judge is ever built, so record whether one actually decided anything.
        # A snapshot naming a model that never ran is the same lie as a policy
        # that was never enforced.
        "judge_used": _uses_judge(parsed),
        "judge_service": judge.get("service"),
        "judge_model": judge.get("model"),
        # A custom judge is a dotted path instead of a service/model pair; with
        # neither recorded the snapshot would say nothing at all about it.
        "judge_factory": judge.get("factory"),
        # The two the dotted path swallowed (#119). `provider` is the name a
        # scenario actually wrote, read back out of the factory it compiled to;
        # `temperature` has no pipecat field at all and rides in `extra`. A
        # snapshot missing them cannot answer "was the same judge asked the
        # same way", which is the only question it exists to answer.
        "judge_provider": _judge_provider(judge),
        "judge_temperature": (judge.get("extra") or {}).get("temperature"),
        # Who *explained*, which is a different question from who decided
        # (#139). Two runs whose reasons came from different models are not
        # comparable, and before this nothing on either row said so.
        **_explainer_fields(judge),
        # Audio only. The caller's voice and the STT the judge read through are
        # as much a part of a result as the judge model: a different TTS says
        # the same line differently, and a different STT mishears it
        # differently. A snapshot without them cannot explain a flip (#72).
        "user_speech": getattr(parsed, "user_speech", None),
        "bot_transcription": getattr(parsed, "transcriber", None),
        "schema_version": scenario_mod.SCHEMA_VERSION,
        # Which code actually executed this, and whether it was the code on
        # disk. A worker started before the feature a scenario depends on
        # produces a result that looks ordinary and is not (#119).
        **worker_build(),
    }


def _judge_provider(judge: dict[str, Any]) -> str | None:
    """Which provider decided the verdicts, by whichever name is recorded.

    A TurnCall-compiled block carries the factory path `PROVIDERS` maps to, so
    the provider is that mapping read backwards. Pipecat's own default carries
    a plain `service:` instead, and an unrecognised factory — a definition that
    predates the closed set — has no provider name to give.
    """
    from turncall.evals.judges import PROVIDER_BY_FACTORY

    factory = judge.get("factory")
    if factory:
        return PROVIDER_BY_FACTORY.get(factory)
    return judge.get("service")


def _explainer_fields(judge: dict[str, Any]) -> dict[str, Any]:
    """Which model supplied the prose behind this run's verdicts (#139).

    Four shapes, all of them pipecat's: `explainer: false` turns reasons off, a
    block names its own model, an absent one on an LLM judge means that judge
    explains — so the honest answer there is its own provider and model, not a
    null — and an absent one on a **classifier** judge means pipecat's own
    default LLM service, which TurnCall only leaves in place for a raw block.
    """
    from turncall.evals.judges import CLASSIFIER_ONLY_JUDGES

    block = judge.get("explainer")
    if block is False:
        return {
            "explainer_used": False,
            "explainer_provider": None,
            "explainer_model": None,
        }
    if isinstance(block, dict):
        return {
            "explainer_used": True,
            "explainer_provider": _judge_provider(block),
            "explainer_model": block.get("model"),
        }
    if _judge_provider(judge) in CLASSIFIER_ONLY_JUDGES:
        # A classifier with no explainer of its own — only reachable through a
        # raw `judge.eval:` block, which wins by design. Pipecat explains that
        # with its own default LLM service, so recording the judge here would
        # claim a classifier wrote prose it cannot write.
        return {
            "explainer_used": True,
            "explainer_provider": "ollama",
            "explainer_model": None,
        }
    return {
        "explainer_used": True,
        "explainer_provider": _judge_provider(judge),
        "explainer_model": judge.get("model"),
    }


def _uses_judge(parsed: Any) -> bool:
    """Whether anything in this scenario actually asks the judge a question."""
    for turn in getattr(parsed, "turns", []) or []:
        if any(getattr(exp, "eval", None) is not None for exp in turn.expect or []):
            return True
    # A simulation is judged end to end by definition (#73).
    return getattr(parsed, "persona", None) is not None


# The tool bridge short-circuits on the scenario's mocks and fails closed on
# the policy (#71), so a snapshot saying `mock_only` now means it. Kept in the
# snapshot rather than dropped: runs written before that slice say `False`, and
# a reader comparing across it needs to know which of the two they are holding.
TOOL_POLICY_ENFORCED = True


def resolved_scenario_snapshot(
    *,
    definition: dict[str, Any],
    schema_version: str,
    tool_mocks: dict[str, Any],
    tool_policy: str,
    judge: dict[str, Any] | None = None,
    simulator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The scenario exactly as it ran, mocks and policy included.

    The mocks are part of what the test means, so a run that does not record
    them cannot be compared with the next one — and whether the policy was
    honoured is part of that, not a detail.
    """
    return {
        "definition": definition,
        "schema_version": schema_version,
        "tool_mocks": tool_mocks,
        "tool_policy": tool_policy,
        "tool_policy_enforced": TOOL_POLICY_ENFORCED,
        # Which LLM decides the verdict is part of what the run means (#118):
        # a result cannot be compared with the next one without it, which is
        # the same argument `harness_config` already makes for the judge model.
        "judge": judge,
        "simulator": simulator,
    }
