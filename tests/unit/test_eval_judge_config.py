"""Choosing the LLM that judges an eval, without handing it an import (#118).

Pipecat builds one judge natively — a local Ollama — and reaches every other
model through `factory`, a dotted path it gives to `importlib.import_module`.
That is fine for a scenario file on someone's disk and unusable for one
arriving in an API body, so TurnCall ships the factories and a request names a
**provider** from a closed set.

The two rules under test: a dotted path never reaches `importlib`, and the
temperature rules that already govern the call path govern this one too.
"""

import pytest

from turncall.api.v1.schemas.evals import CreateEvalScenarioRequest, EvalModelSchema
from turncall.evals.judges import PROVIDERS
from turncall.evals.scenario import (
    ScenarioError,
    compile_model_block,
    reject_factories,
    with_models,
)

pytestmark = pytest.mark.unit

SCRIPTED = {"turns": [{"user": "hi", "expect": [{"event": "llm_response"}]}]}


class TestAFactoryIsNeverImported:
    @pytest.mark.parametrize(
        "definition",
        [
            {"judge": {"eval": {"factory": "os.system"}}},
            {"judge": {"transcription": {"factory": "os.system"}}},
            {"simulator": {"factory": "os.system"}},
            {"user": {"speech": {"factory": "os.system"}}},
        ],
    )
    def test_every_block_pipecat_imports_from_is_refused(self, definition) -> None:
        with pytest.raises(ScenarioError, match="factory is not accepted"):
            reject_factories(definition)

    def test_the_refusal_says_what_to_write_instead(self) -> None:
        with pytest.raises(ScenarioError) as caught:
            reject_factories({"simulator": {"factory": "evil.module"}})
        assert "provider" in str(caught.value)
        for provider in PROVIDERS:
            assert provider in str(caught.value)

    def test_a_definition_without_one_passes(self) -> None:
        reject_factories({"judge": {"eval": {"service": "ollama"}}, **SCRIPTED})

    def test_the_api_rejects_it_on_the_way_in(self) -> None:
        """The guard runs before pipecat's parser, because this one is about
        what the parser would *import*, not what it would accept."""
        with pytest.raises(ValueError, match="factory is not accepted"):
            CreateEvalScenarioRequest(
                name="smuggled",
                definition={**SCRIPTED, "judge": {"eval": {"factory": "os.system"}}},
            )

    def test_the_typed_block_has_nowhere_to_put_one(self) -> None:
        with pytest.raises(ValueError):
            EvalModelSchema(provider="openai", factory="os.system")

    def test_only_shipped_callables_are_reachable(self) -> None:
        """The mapping is the allowlist: every value names something in this
        repo, so a compiled block can only point at code we wrote."""
        assert all(
            path.startswith("turncall.evals.judges.") for path in PROVIDERS.values()
        )
        with pytest.raises(ScenarioError, match="unknown judge provider"):
            compile_model_block({"provider": "together"})


class TestTheCompiledBlock:
    def test_a_provider_becomes_the_factory_we_ship(self) -> None:
        compiled = compile_model_block({"provider": "openai", "model": "gpt-4o"})
        assert compiled == {
            "factory": "turncall.evals.judges.openai",
            "model": "gpt-4o",
        }

    def test_ollama_is_the_default_and_keeps_its_endpoint(self) -> None:
        compiled = compile_model_block({"endpoint": "http://gpu-box:11434/v1"})
        assert compiled["factory"] == "turncall.evals.judges.ollama_judge"
        assert compiled["endpoint"] == "http://gpu-box:11434/v1"

    def test_a_simulator_gets_the_plain_llm_not_the_judges_classifier(self) -> None:
        """A persona is linked into a pipeline to speak. The judge's ollama is
        an `LLMClassifier` wrapper (for its wider timeout), and handing that to
        a simulator raises `AttributeError: no attribute 'link'` at build."""
        judge = compile_model_block({"provider": "ollama"}, role="judge")
        simulator = compile_model_block({"provider": "ollama"}, role="simulator")

        assert judge["factory"] == "turncall.evals.judges.ollama_judge"
        assert simulator["factory"] == "turncall.evals.judges.ollama"

    def test_temperature_rides_in_extra_because_pipecat_has_no_field(self) -> None:
        compiled = compile_model_block({"provider": "openai", "temperature": 0.2})
        assert compiled["extra"] == {"temperature": 0.2}

    @pytest.mark.parametrize(
        "block",
        [
            {"provider": "anthropic", "model": "claude-sonnet-5", "temperature": 0.3},
            {"provider": "openai", "model": "o3-mini", "temperature": 0.3},
            {"provider": "openai", "model": "gpt-5", "temperature": 0.3},
        ],
    )
    def test_a_model_that_rejects_temperature_is_sent_none(self, block) -> None:
        """The same 400 that made the call path unusable — the rule travels
        with the model, not with whoever configured it."""
        assert "extra" not in compile_model_block(block)

    def test_nothing_configured_compiles_to_nothing(self) -> None:
        assert compile_model_block(None) is None
        assert compile_model_block({}) is None


class TestApplyingItToADefinition:
    def test_the_judge_reaches_pipecats_block(self) -> None:
        merged = with_models(SCRIPTED, judge={"provider": "openai", "model": "gpt-4o"})
        assert merged["judge"]["eval"]["factory"] == "turncall.evals.judges.openai"

    def test_the_persona_reaches_its_own(self) -> None:
        merged = with_models(SCRIPTED, simulator={"provider": "openai"})
        assert merged["simulator"]["factory"] == "turncall.evals.judges.openai"

    def test_a_raw_block_in_the_definition_wins(self) -> None:
        """It was there first, and a stored scenario's verdicts must not start
        being decided by a different model because a field appeared beside it."""
        definition = {**SCRIPTED, "judge": {"eval": {"service": "ollama"}}}
        merged = with_models(definition, judge={"provider": "openai"})
        assert merged["judge"]["eval"] == {"service": "ollama"}

    def test_the_input_is_not_mutated(self) -> None:
        definition = {**SCRIPTED}
        with_models(definition, judge={"provider": "openai"})
        assert "judge" not in definition

    def test_no_configuration_leaves_the_definition_alone(self) -> None:
        assert with_models(SCRIPTED) == SCRIPTED


class TestTheRunRecordsIt:
    def test_the_snapshot_carries_the_judge(self) -> None:
        """A verdict cannot be compared with the next one without knowing which
        model answered — the argument `harness_config` already makes."""
        from turncall.evals.runner import resolved_scenario_snapshot

        snapshot = resolved_scenario_snapshot(
            definition=SCRIPTED,
            schema_version="pipecat-1.11",
            tool_mocks={},
            tool_policy="mock_only",
            judge={"provider": "openai", "model": "gpt-4o"},
        )
        assert snapshot["judge"] == {"provider": "openai", "model": "gpt-4o"}
        assert snapshot["simulator"] is None

    def test_the_run_parses_with_the_judge_applied(self) -> None:
        from types import SimpleNamespace

        from turncall.domain.enums import EvalModality
        from turncall.evals.runner import _parse_for_run

        run = SimpleNamespace(
            scenario_name="judged",
            resolved_scenario={
                "definition": SCRIPTED,
                "judge": {"provider": "openai", "model": "gpt-4o"},
            },
        )
        _kind, parsed = _parse_for_run(run, EvalModality.TEXT)
        # Pipecat lifts `judge.eval` onto the scenario as `judge`, so this is
        # the block the harness will build the judge from.
        assert parsed.judge["factory"] == "turncall.evals.judges.openai"
        assert parsed.judge["model"] == "gpt-4o"
