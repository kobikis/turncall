"""Eval request-model validation, asserted directly.

A scenario's definition is pipecat's, so validation is a round-trip through
pipecat's parser: what a run will later build is exactly what is checked at
create, and pipecat's own error — which names the offending field — is what the
author gets back.
"""

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.evals import (
    CreateEvalRunRequest,
    CreateEvalScenarioRequest,
    EvalTarget,
    UpdateEvalScenarioRequest,
)
from turncall.domain.enums import EvalKind, EvalModality, EvalToolPolicy
from turncall.evals.scenario import ScenarioError, kind_of, with_modality

pytestmark = pytest.mark.unit

SCRIPTED = {
    "turns": [
        {"user": "hello", "expect": [{"event": "llm_response", "text_contains": "hi"}]}
    ]
}
SIMULATION = {
    "persona": "a caller who lost their booking reference",
    "goal": "recover the booking",
    "success": "the agent finds the booking",
}


class TestKindIsComputedEagerly:
    def test_turns_is_scripted(self) -> None:
        body = CreateEvalScenarioRequest(name="greets", definition=SCRIPTED)
        assert body.kind is EvalKind.SCRIPTED

    def test_persona_is_a_simulation(self) -> None:
        body = CreateEvalScenarioRequest(name="recovers", definition=SIMULATION)
        assert body.kind is EvalKind.SIMULATION

    def test_both_kinds_at_once_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not both"):
            CreateEvalScenarioRequest(
                name="confused", definition={**SCRIPTED, **SIMULATION}
            )

    def test_neither_kind_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="needs 'turns'"):
            CreateEvalScenarioRequest(name="empty", definition={"judge": {}})


class TestDefinitionValidation:
    def test_pipecats_own_parser_error_is_surfaced(self) -> None:
        """The author is better served by the message naming the field than by
        anything we could paraphrase."""
        with pytest.raises(ValidationError, match="'turns:' must be a list"):
            CreateEvalScenarioRequest(name="bad", definition={"turns": "nope"})

    def test_a_malformed_turn_is_caught_at_create(self) -> None:
        with pytest.raises(ValidationError):
            CreateEvalScenarioRequest(
                name="bad-turn",
                definition={"turns": [{"user": "hi", "expect": "not a list"}]},
            )

    def test_an_update_without_a_definition_skips_the_check(self) -> None:
        body = UpdateEvalScenarioRequest(description="just a note")
        assert body.definition is None

    def test_an_update_with_a_bad_definition_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UpdateEvalScenarioRequest(definition={"turns": "nope"})


class TestDefaults:
    def test_tool_policy_defaults_to_failing_closed(self) -> None:
        """A scenario pointed at a real agent must not book a real appointment
        on every iteration just because nobody set a policy."""
        body = CreateEvalScenarioRequest(name="greets", definition=SCRIPTED)
        assert body.tool_policy is EvalToolPolicy.MOCK_ONLY
        assert body.tool_mocks == {}


class TestTarget:
    def test_an_agent_target_is_accepted(self) -> None:
        from uuid import uuid4

        target = EvalTarget(type="agent", agent_id=uuid4())
        assert target.agent_id is not None

    def test_an_agent_target_without_an_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="needs an 'agent_id'"):
            EvalTarget(type="agent")

    def test_the_unbuilt_target_types_are_rejected_at_the_boundary(self) -> None:
        """Better a 422 than a run that is accepted and then errors in a
        worker the caller cannot see."""
        for spec in ({"type": "agent_name", "name": "support"}, {"type": "inline"}):
            with pytest.raises(ValidationError, match="not supported yet"):
                EvalTarget(**spec)

    def test_an_unknown_target_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvalTarget(type="carrier-pigeon")


class TestRunRequest:
    def test_defaults_are_text_and_one_iteration(self) -> None:
        from uuid import uuid4

        body = CreateEvalRunRequest(
            scenario_id=uuid4(), target=EvalTarget(type="agent", agent_id=uuid4())
        )
        assert body.modality is EvalModality.TEXT
        assert body.iterations == 1

    def test_audio_is_rejected_until_its_slice_lands(self) -> None:
        from uuid import uuid4

        with pytest.raises(ValidationError, match="audio modality is not supported"):
            CreateEvalRunRequest(
                scenario_id=uuid4(),
                target=EvalTarget(type="agent", agent_id=uuid4()),
                modality="audio",
            )

    def test_zero_iterations_is_rejected(self) -> None:
        from uuid import uuid4

        with pytest.raises(ValidationError):
            CreateEvalRunRequest(
                scenario_id=uuid4(),
                target=EvalTarget(type="agent", agent_id=uuid4()),
                iterations=0,
            )


class TestModalityMerge:
    def test_the_run_sets_both_sides(self) -> None:
        merged = with_modality(SCRIPTED, EvalModality.TEXT)
        assert merged["user"]["modality"] == "text"
        assert merged["judge"]["modality"] == "text"

    def test_a_scenarios_own_modality_wins(self) -> None:
        """The asymmetric pair — user audio, judge text — stays reachable
        through the scenario's own blocks."""
        definition = {**SCRIPTED, "user": {"modality": "audio"}}
        merged = with_modality(definition, EvalModality.TEXT)
        assert merged["user"]["modality"] == "audio"
        assert merged["judge"]["modality"] == "text"

    def test_the_original_definition_is_not_mutated(self) -> None:
        definition = {**SCRIPTED}
        with_modality(definition, EvalModality.AUDIO)
        assert "judge" not in definition


class TestKindOf:
    def test_it_raises_a_scenario_error_not_a_key_error(self) -> None:
        with pytest.raises(ScenarioError):
            kind_of({})
