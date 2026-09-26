"""A simulation, end to end (#73).

A scripted scenario asks "at this point, did the agent make the right next
decision?"; a simulation asks "by the end, did it reach the right outcome?" —
an LLM plays the caller and improvises, and the judge rules on the goal once
over the whole conversation.

Live, and it needs **Ollama reachable**, not just provider keys: pipecat's
persona LLM and its judge both default to a local Ollama model (ADR-0018 —
`service: openai` is deprecated in pipecat 1.9 and gone in 2.0). Skipped when
nothing answers on OLLAMA_HOST. Three nondeterministic actors — persona, agent
and judge — so this asserts the machinery ran, not a particular wording.
"""

import os
from uuid import uuid4

import httpx
import pytest

from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget, map_simulation_result

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


@pytest.fixture
def ollama_model() -> str:
    """A model this machine actually has.

    Pipecat's default is `gemma4:12b` for both the persona and the judge, and
    an Ollama that is running without it fails the run with
    `404 model 'gemma4:12b' not found` — which the runner maps to `errored`
    naming the model, correctly, but proves nothing about a simulation. So the
    scenario names whatever is installed rather than assuming pipecat's
    default. `EVAL_OLLAMA_MODEL` overrides when a box has several and one of
    them supports tool calling (the persona ends the call with an `end_call`
    tool; without tool support it runs to `max_turns` instead, which this test
    allows).
    """
    try:
        tags = httpx.get(f"{OLLAMA}/api/tags", timeout=2.0)
        tags.raise_for_status()
        installed = [m["name"] for m in tags.json().get("models", [])]
    except Exception:
        pytest.skip(
            f"no Ollama at {OLLAMA}: pipecat's persona and judge both default to it"
        )
    if not installed:
        pytest.skip(f"Ollama at {OLLAMA} has no models pulled")
    chosen = os.environ.get("EVAL_OLLAMA_MODEL") or installed[0]
    if chosen not in installed:
        pytest.skip(f"EVAL_OLLAMA_MODEL={chosen!r} is not pulled: {installed}")
    return chosen


@pytest.fixture
def session_factory():
    from turncall.config.settings import Settings
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_a_simulation_holds_a_conversation_and_is_judged(
    openai_key: str, ollama_model: str, session_factory
) -> None:
    from turncall.config.settings import Settings

    definition = {
        "persona": (
            "A polite traveller who asks one short question and then says "
            "goodbye. Never asks more than two questions."
        ),
        "goal": "Find out the capital of France, then end the call.",
        "success": "the agent told the caller that the capital of France is Paris",
        "metrics": [{"name": "politeness", "criterion": "the agent stayed courteous"}],
        "max_turns": 4,
    }
    blob = {
        "system_prompt": "You are a terse, courteous geography assistant.",
        "first_message": None,
        "llm": {"provider": "openai", "model": "gpt-4o-mini"},
        "smart_turn_detection": False,
    }
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )

    # TurnCall's typed blocks (#118), applied the way `runner` applies them —
    # they are columns beside the definition, not keys inside it, and only
    # this path reaches TurnCall's own factories. Both models default to
    # pipecat's `gemma4:12b`, which this box may not have.
    #
    # Going through the factories became load-bearing in pipecat 1.12. A raw
    # `judge.eval` block inside the definition is wrapped in an
    # `LLMClassifier` carrying pipecat's default 10s budget, while 1.12 judges
    # a simulation one bot turn per call and fires those calls together —
    # four concurrent classifications take ~18s each against a local Ollama,
    # so every one of them times out and the run reports "judge call failed".
    # TurnCall's `ollama` factory widens that budget; a raw block cannot.
    definition = scenario_mod.with_models(
        definition,
        judge={"provider": "ollama", "model": ollama_model},
        simulator={"provider": "ollama", "model": ollama_model},
    )
    merged = scenario_mod.with_modality(definition, EvalModality.TEXT)
    parsed = scenario_mod.parse(merged, name="live-simulation")
    result = await run_iteration(
        parsed=parsed,
        kind=EvalKind.SIMULATION,
        target=target,
        modality=EvalModality.TEXT,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=None,
    )

    outcome = map_simulation_result(result, iteration=1)
    entry = outcome.entry

    # The machinery, not the wording: three nondeterministic actors means a
    # specific verdict is not a stable assertion. What must hold is that a
    # conversation happened and the judge ruled on it.
    assert entry["error"] is None, entry["error"]
    assert entry["persona_turns"] >= 1
    assert [m["role"] for m in entry["transcript"]].count("assistant") >= 1
    assert entry["goal"]["reason"], "the judge has to say why, either way"
    assert entry["ended_by"] in (
        "end_call",
        "bot",
        "max_turns",
        "max_duration",
        "silence",
    )
    assert [m["name"] for m in entry["metrics"]] == ["politeness"]
    # A metric with no min_score reports and fails nothing.
    assert entry["metrics"][0]["min_score"] is None
    if entry["persona_claim"] is not None:
        assert entry["persona_claim"]["advisory"] is True
