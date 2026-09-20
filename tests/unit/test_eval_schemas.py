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


class TestToolMocksAndPolicy:
    """#71: the tool bridge short-circuits on the mocks and fails closed on the
    policy, so the boundary can accept both."""

    def test_mocks_and_policy_are_accepted(self) -> None:
        body = CreateEvalScenarioRequest(
            name="books",
            definition=SCRIPTED,
            tool_mocks={"book": {"status": "success", "id": "APT-1"}},
            tool_policy=EvalToolPolicy.MOCK_ONLY,
        )
        assert body.tool_mocks == {"book": {"status": "success", "id": "APT-1"}}
        assert body.tool_policy is EvalToolPolicy.MOCK_ONLY

    def test_live_is_accepted_but_has_to_be_typed(self) -> None:
        """The dangerous path is opt-in; omitting the field is not it."""
        assert (
            UpdateEvalScenarioRequest(tool_policy=EvalToolPolicy.LIVE).tool_policy
            is EvalToolPolicy.LIVE
        )
        assert (
            CreateEvalScenarioRequest(name="greets", definition=SCRIPTED).tool_policy
            is None
        )

    def test_a_key_that_is_not_a_tool_name_is_rejected(self) -> None:
        """The key is matched against a tool name at dispatch; one that cannot
        be a tool name would sit in the row mocking nothing."""
        with pytest.raises(ValidationError, match="is not a tool name"):
            CreateEvalScenarioRequest(
                name="books", definition=SCRIPTED, tool_mocks={"not a name": {}}
            )

    def test_an_mcp_style_camelcase_name_is_still_accepted(self) -> None:
        """An MCP server names its own tools, and camelCase is common there."""
        body = CreateEvalScenarioRequest(
            name="books", definition=SCRIPTED, tool_mocks={"bookAppointment": {}}
        )
        assert body.tool_mocks == {"bookAppointment": {}}

    def test_a_mock_over_the_tool_result_limit_is_rejected(self) -> None:
        """A mock is handed to the model as a tool result and occupies the
        context for the rest of the conversation, so it is held to the same
        limit a real one is. Truncating it silently would make the scenario
        mean something other than what it says."""
        with pytest.raises(ValidationError, match="over the"):
            CreateEvalScenarioRequest(
                name="books",
                definition=SCRIPTED,
                tool_mocks={"book": "z" * 2_000_000},
            )

    def test_too_many_mocks_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="more than"):
            CreateEvalScenarioRequest(
                name="books",
                definition=SCRIPTED,
                tool_mocks={f"tool_{i}": {} for i in range(65)},
            )

    def test_an_update_is_bounded_the_same_way(self) -> None:
        with pytest.raises(ValidationError, match="is not a tool name"):
            UpdateEvalScenarioRequest(tool_mocks={"not a name": {}})

    def test_an_unknown_policy_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CreateEvalScenarioRequest(
                name="greets", definition=SCRIPTED, tool_policy="whatever"
            )


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

    def test_audio_is_accepted(self) -> None:
        """#72. Text stays the default — it is what people run per PR — but
        audio is the mode that covers what makes this a voice platform."""
        from uuid import uuid4

        body = CreateEvalRunRequest(
            scenario_id=uuid4(),
            target=EvalTarget(type="agent", agent_id=uuid4()),
            modality="audio",
        )
        assert body.modality is EvalModality.AUDIO

    def test_an_unknown_modality_is_still_rejected(self) -> None:
        from uuid import uuid4

        with pytest.raises(ValidationError):
            CreateEvalRunRequest(
                scenario_id=uuid4(),
                target=EvalTarget(type="agent", agent_id=uuid4()),
                modality="video",
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

    def test_audio_brings_the_transcription_pipecat_demands(self) -> None:
        """#72: pipecat *raises* on `judge.modality: audio` with no
        `transcription:` block, so a run asking for audio has to bring a
        default or every audio scenario is unrunnable. Proven by parsing it."""
        from turncall.evals.scenario import parse

        merged = with_modality(SCRIPTED, EvalModality.AUDIO)
        assert merged["judge"]["transcription"] == {"service": "moonshine"}
        assert merged["user"]["speech"] == {"service": "kokoro", "voice": "af_heart"}
        parsed = parse(merged, name="greets")
        assert parsed.bot_audio is True
        assert parsed.user_audio is True
        assert parsed.transcriber["service"] == "moonshine"
        assert parsed.user_speech["service"] == "kokoro"

    def test_a_scenarios_own_transcriber_is_left_alone(self) -> None:
        definition = {
            **SCRIPTED,
            "judge": {"transcription": {"service": "whisper", "model": "base"}},
        }
        merged = with_modality(definition, EvalModality.AUDIO)
        assert merged["judge"]["transcription"] == {
            "service": "whisper",
            "model": "base",
        }

    def test_a_scenarios_own_voice_is_left_alone(self) -> None:
        definition = {**SCRIPTED, "user": {"speech": {"factory": "mine.voice"}}}
        merged = with_modality(definition, EvalModality.AUDIO)
        assert merged["user"]["speech"] == {"factory": "mine.voice"}

    def test_text_gets_neither_service(self) -> None:
        """They would be built for nothing — and pipecat's text path never
        reads either one."""
        merged = with_modality(SCRIPTED, EvalModality.TEXT)
        assert "transcription" not in merged["judge"]
        assert "speech" not in merged["user"]

    def test_the_original_definition_is_not_mutated(self) -> None:
        definition = {**SCRIPTED}
        with_modality(definition, EvalModality.AUDIO)
        assert "judge" not in definition


class TestKindOf:
    def test_it_raises_a_scenario_error_not_a_key_error(self) -> None:
        with pytest.raises(ScenarioError):
            kind_of({})
