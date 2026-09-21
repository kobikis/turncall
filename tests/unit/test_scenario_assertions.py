"""A scenario that cannot fail has to say so (#95).

Validation is a round-trip through pipecat's parser: it checks *shape*, not
strength. A scenario whose every expectation is a bare `{"event":
"llm_response"}` parses cleanly, stores, runs, and reports `passed` forever —
including against an agent whose LLM returns nothing, because a provider that
404s still emits an empty `llm_response`. That is not a corner case: it is the
#63/#64/#65 class the feature was built on.

Advisory, never fatal. `_warn_unmatched_mocks` set that precedent and the
asymmetric cases are real.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.evals.scenario import assertion_warnings

pytestmark = pytest.mark.unit


def _scripted(*expectations):
    return {
        "turns": [
            {"user": f"turn {i}", "expect": [expectation]}
            for i, expectation in enumerate(expectations, start=1)
        ]
    }


def _codes(definition):
    return [w["code"] for w in assertion_warnings(definition, name="scenario")]


class TestTheLoudCase:
    def test_a_bare_event_cannot_fail_and_says_so(self) -> None:
        assert _codes(_scripted({"event": "llm_response"})) == ["scenario_cannot_fail"]

    def test_the_message_names_the_provider_case_it_hides(self) -> None:
        """Whoever reads this needs to know *why* green means nothing here."""
        warning = assertion_warnings(_scripted({"event": "llm_response"}))[0]
        assert "llm_response" in warning["message"]
        assert "text_contains" in warning["message"], "and what to do about it"
        assert warning["expectations"] == ["turn 1: llm_response"]

    def test_turns_with_no_expectations_at_all(self) -> None:
        assert _codes({"turns": [{"user": "hi", "expect": []}]}) == [
            "scenario_cannot_fail"
        ]


class TestWhatCountsAsAnAssertion:
    @pytest.mark.parametrize(
        "expectation",
        [
            {"event": "llm_response", "text_contains": "Berlin"},
            {"event": "llm_response", "text_excludes": "sorry"},
            {"event": "llm_response", "eval": "answers the question"},
            {"event": "function_call", "calls": [{"name": "book", "args": {}}]},
            # Asserting an event never arrives is a claim about behaviour, and
            # pipecat forbids combining it with the content checks.
            {"event": "function_call", "absent": True},
        ],
    )
    def test_a_real_assertion_warns_about_nothing(self, expectation) -> None:
        assert _codes(_scripted(expectation)) == []

    def test_a_function_call_only_scenario_is_fine(self) -> None:
        """Asserting a call with its arguments is a real assertion — this is
        why the check warns rather than rejects."""
        assert (
            _codes(
                _scripted(
                    {"event": "function_call", "calls": [{"name": "book", "args": {}}]},
                    {"event": "function_call_stopped", "calls": [{"name": "book"}]},
                )
            )
            == []
        )

    def test_a_simulation_is_exempt(self) -> None:
        """Its judge rules on `success` over the whole conversation, so there
        is always something to fail.

        The mapping has to be one pipecat actually accepts — `persona`, `goal`
        and `success` are top-level strings — or this would pass for the wrong
        reason, since a definition that cannot parse also yields no warnings.
        """
        simulation = {
            "persona": "a caller booking a table",
            "goal": "book a table for two",
            "success": "the agent books a table for two",
        }
        from turncall.evals.scenario import parse

        assert parse(simulation, name="booking").persona, "the fixture must parse"
        assert _codes(simulation) == []

    def test_a_definition_that_does_not_parse_is_the_validators_problem(self) -> None:
        assert _codes({"turns": "not a list"}) == []


class TestTheMixedCase:
    def test_some_weak_expectations_are_named_not_hidden(self) -> None:
        codes = _codes(
            _scripted(
                {"event": "llm_response"},
                {"event": "llm_response", "text_contains": "Berlin"},
            )
        )
        assert codes == ["content_free_expectations"]

    def test_the_count_and_the_turns_are_in_the_message(self) -> None:
        warning = assertion_warnings(
            _scripted(
                {"event": "llm_response"},
                {"event": "llm_response", "text_contains": "x"},
            )
        )[0]
        assert "1 of 2" in warning["message"]
        assert warning["expectations"] == ["turn 1: llm_response"]


class TestItReachesBothSurfaces:
    def test_the_scenario_response_carries_it(self) -> None:
        """`POST`/`PUT /v1/eval-scenarios` — derived on read, so a scenario
        trimmed into this state after it was created says so too."""
        from datetime import UTC, datetime

        from turncall.api.v1.schemas.evals import EvalScenarioResponse

        response = EvalScenarioResponse.model_validate(
            SimpleNamespace(
                id=uuid4(),
                project_id=uuid4(),
                name="greets",
                description=None,
                kind="script",
                definition=_scripted({"event": "llm_response"}),
                schema_version="pipecat-1.11",
                tool_mocks={},
                tool_policy="mock_only",
                tags=[],
                default_target=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        assert [w["code"] for w in response.warnings] == ["scenario_cannot_fail"]

    @pytest.mark.asyncio
    async def test_the_run_carries_it_too(self) -> None:
        """The person reading a green verdict is not always the one who wrote
        the scenario."""
        from turncall.domain.models import AgentConfig
        from turncall.evals import runner as runner_mod
        from turncall.evals.runner import ResolvedTarget

        run = SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            status="queued",
            modality="text",
            kind="script",
            iterations=1,
            scenario_name="greets",
            scenario_id=uuid4(),
            batch_id=None,
            target={"type": "agent", "agent_id": str(uuid4())},
            resolved_scenario={"definition": _scripted({"event": "llm_response"})},
            warnings=[],
        )
        start = AsyncMock()
        target = ResolvedTarget(
            project_id=run.project_id,
            config=AgentConfig(),
            config_blob={},
            agent_id=uuid4(),
        )
        with (
            patch("turncall.storage.repositories.eval_repo.start_run", start),
            patch.object(runner_mod, "resolve_target", AsyncMock(return_value=target)),
            patch.object(runner_mod, "_dispatch_run_event", AsyncMock()),
        ):
            await runner_mod._plan_run(
                AsyncMock(),
                run,
                settings=SimpleNamespace(),
                session_factory=MagicMock(),
            )

        written = [w["code"] for w in start.await_args.kwargs["warnings"]]
        assert written == ["scenario_cannot_fail"]
