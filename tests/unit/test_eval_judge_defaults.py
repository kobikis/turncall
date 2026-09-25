"""A judge set once, and a verdict that says which judge decided it (#119).

#118 made the judge a typed block on the scenario. Two things were left: there
was nowhere to set one for a whole project, so every scenario repeated it and
drifted apart by hand; and nothing acted on the snapshot that records it, so a
scenario that passed yesterday and fails today read identically whether the
agent regressed or the judge changed underneath it.

Precedence is narrowest-wins, and blocks are taken **whole**: the scenario's,
then the platform's, then pipecat's local Ollama.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.domain.enums import EvalModality
from turncall.evals.judges import default_block
from turncall.evals.runner import _parse_for_run, _warn_judge_changed, harness_config

pytestmark = pytest.mark.unit

SCRIPTED = {"turns": [{"user": "hi", "expect": [{"event": "llm_response"}]}]}
OPENAI_FACTORY = "turncall.evals.judges.openai"
SIMULATION = {
    "persona": "a caller who lost their booking reference",
    "goal": "recover the booking",
    "success": "the agent finds the booking",
}


def _settings(**over):
    """Settings carrying only the fields the resolver reads."""
    evals = dict(
        judge_provider=None,
        judge_model=None,
        judge_temperature=None,
        simulator_provider=None,
        simulator_model=None,
        simulator_temperature=None,
    )
    evals.update(over)
    return SimpleNamespace(evals=SimpleNamespace(**evals))


def _run(**over):
    resolved = {"definition": SCRIPTED}
    resolved.update(over.pop("resolved_scenario", {}))
    fields = dict(
        id=uuid4(),
        scenario_id=uuid4(),
        scenario_name="greets",
        resolved_scenario=resolved,
    )
    fields.update(over)
    return SimpleNamespace(**fields)


class TestThePlatformDefault:
    def test_a_scenario_with_no_block_takes_it(self) -> None:
        _kind, parsed = _parse_for_run(
            _run(),
            EvalModality.TEXT,
            _settings(judge_provider="openai", judge_model="gpt-4o"),
        )
        assert parsed.judge["factory"] == OPENAI_FACTORY
        assert parsed.judge["model"] == "gpt-4o"

    def test_a_scenario_with_its_own_block_keeps_it(self) -> None:
        """Narrowest wins. The scenario is the narrower statement, and the one
        its author can see next to the assertions it decides."""
        _kind, parsed = _parse_for_run(
            _run(
                resolved_scenario={"judge": {"provider": "ollama", "model": "gemma3"}}
            ),
            EvalModality.TEXT,
            _settings(judge_provider="openai", judge_model="gpt-4o"),
        )
        assert parsed.judge.get("factory") == "turncall.evals.judges.ollama"
        assert parsed.judge["model"] == "gemma3"

    def test_the_block_is_taken_whole_not_merged(self) -> None:
        """A platform `model` landing on a scenario's provider would name a
        model that provider has never heard of."""
        _kind, parsed = _parse_for_run(
            _run(resolved_scenario={"judge": {"provider": "anthropic"}}),
            EvalModality.TEXT,
            _settings(judge_provider="openai", judge_model="gpt-4o"),
        )
        assert parsed.judge["factory"] == "turncall.evals.judges.anthropic"
        assert parsed.judge.get("model") is None

    def test_with_neither_set_pipecats_ollama_still_decides(self) -> None:
        _kind, parsed = _parse_for_run(_run(), EvalModality.TEXT, _settings())
        # Untouched: no factory of ours, and pipecat's own default filled in.
        assert "factory" not in (parsed.judge or {})
        assert (parsed.judge or {}).get("service") == "ollama"

    def test_a_temperature_alone_configures_the_default_provider(self) -> None:
        assert default_block(None, None, 0.2) == {
            "provider": "ollama",
            "model": None,
            "temperature": 0.2,
        }

    def test_nothing_configured_is_no_block_at_all(self) -> None:
        assert default_block(None, None, None) is None

    def test_the_simulator_has_its_own_pair(self) -> None:
        run = _run(resolved_scenario={"definition": SIMULATION})
        _kind, parsed = _parse_for_run(
            run, EvalModality.TEXT, _settings(simulator_provider="openai")
        )
        assert parsed.simulator["factory"] == OPENAI_FACTORY


class TestTheSnapshotIsComplete:
    def test_it_carries_provider_model_and_temperature(self) -> None:
        _kind, parsed = _parse_for_run(
            _run(),
            EvalModality.TEXT,
            _settings(
                judge_provider="openai", judge_model="gpt-4o", judge_temperature=0.3
            ),
        )
        snapshot = harness_config(parsed)
        assert snapshot["judge_provider"] == "openai"
        assert snapshot["judge_model"] == "gpt-4o"
        assert snapshot["judge_temperature"] == 0.3
        assert snapshot["judge_factory"] == OPENAI_FACTORY

    def test_pipecats_own_judge_reports_its_service_as_the_provider(self) -> None:
        _kind, parsed = _parse_for_run(_run(), EvalModality.TEXT, _settings())
        snapshot = harness_config(parsed)
        assert snapshot["judge_provider"] == "ollama"
        assert snapshot["judge_temperature"] is None

    def test_anthropic_records_no_temperature_because_it_is_never_sent(self) -> None:
        """The snapshot says what ran. Claude rejects the parameter outright,
        so recording one would describe a request nobody made."""
        _kind, parsed = _parse_for_run(
            _run(),
            EvalModality.TEXT,
            _settings(judge_provider="anthropic", judge_temperature=0.5),
        )
        assert harness_config(parsed)["judge_temperature"] is None


class TestAChangedJudgeIsAnnounced:
    @staticmethod
    def _previous(harness):
        return patch(
            "turncall.storage.repositories.eval_repo.last_judged_harness",
            AsyncMock(return_value=harness),
        )

    @pytest.mark.asyncio
    async def test_a_different_judge_warns_naming_both(self) -> None:
        current = {"judge_provider": "openai", "judge_model": "gpt-4o"}
        with self._previous({"judge_provider": "ollama", "judge_model": "gemma3"}):
            warnings = await _warn_judge_changed(AsyncMock(), _run(), current)

        assert [w["code"] for w in warnings] == ["judge_changed"]
        message = warnings[0]["message"]
        assert "ollama" in message and "openai" in message
        assert warnings[0]["judge"]["judge_model"] == {
            "previous": "gemma3",
            "current": "gpt-4o",
        }

    @pytest.mark.asyncio
    async def test_the_same_judge_says_nothing(self) -> None:
        same = {"judge_provider": "openai", "judge_model": "gpt-4o"}
        with self._previous(dict(same)):
            assert await _warn_judge_changed(AsyncMock(), _run(), same) == []

    @pytest.mark.asyncio
    async def test_the_first_run_of_a_scenario_carries_no_warning(self) -> None:
        with self._previous(None):
            assert await _warn_judge_changed(AsyncMock(), _run(), {}) == []

    @pytest.mark.asyncio
    async def test_an_inline_scenario_has_no_history_to_differ_from(self) -> None:
        """#77: nothing is stored, so there is no earlier run of *this*
        scenario — and the query would be against a null id."""
        with self._previous({"judge_model": "gemma3"}) as query:
            assert (
                await _warn_judge_changed(AsyncMock(), _run(scenario_id=None), {}) == []
            )
        query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_key_the_older_snapshot_never_recorded_is_not_a_change(
        self,
    ) -> None:
        """`judge_provider` and `judge_temperature` arrived with this slice.
        Comparing against their absence would announce a judge change on the
        next run of every scenario in the database."""
        previous = {"judge_service": "ollama", "judge_model": "gemma3"}
        current = {
            "judge_service": "ollama",
            "judge_model": "gemma3",
            "judge_provider": "ollama",
            "judge_temperature": None,
        }
        with self._previous(previous):
            assert await _warn_judge_changed(AsyncMock(), _run(), current) == []


class TestARunCannotSwapTheJudge:
    def test_a_judge_on_the_run_request_is_refused(self) -> None:
        """Refused, not ignored: a run is what it was queued as, and silently
        dropping the field leaves the author believing it applied."""
        from pydantic import ValidationError

        from turncall.api.v1.schemas.evals import CreateEvalRunRequest

        with pytest.raises(ValidationError, match="judge"):
            CreateEvalRunRequest(
                scenario_id=uuid4(),
                target={"type": "agent_name", "name": "support"},
                judge={"provider": "openai"},
            )

    def test_an_inline_scenario_may_still_carry_one(self) -> None:
        """It is a scenario, not a run — the file on disk is the same thing the
        stored row is."""
        from turncall.api.v1.schemas.evals import CreateEvalRunRequest

        body = CreateEvalRunRequest(
            scenario={
                "name": "from a file",
                "definition": SCRIPTED,
                "judge": {"provider": "openai", "model": "gpt-4o"},
            },
            target={"type": "agent_name", "name": "support"},
        )
        assert body.scenario.judge.model == "gpt-4o"


class TestAScenarioCanGoBackToTheDefault:
    """A judge has to be removable, or the Console's "inherit the platform
    default" is a lie the moment anyone picks a model (#119, builder-web#26).
    """

    @staticmethod
    def _values(**sent):
        from turncall.api.v1.evals import _update_values
        from turncall.api.v1.schemas.evals import UpdateEvalScenarioRequest

        return _update_values(UpdateEvalScenarioRequest(**sent))

    def test_an_explicit_null_clears_it(self) -> None:
        assert self._values(judge=None) == {"judge": None}

    def test_not_sending_it_leaves_it_alone(self) -> None:
        assert "judge" not in self._values(name="renamed")

    def test_a_block_is_stored_compiled(self) -> None:
        values = self._values(judge={"provider": "openai", "model": "gpt-4o"})
        assert values["judge"] == {"provider": "openai", "model": "gpt-4o"}

    def test_a_null_name_is_still_not_a_rename(self) -> None:
        """The exception is the two fields where absence is a real value, not
        a general "nulls now clear things"."""
        assert self._values(name=None, judge=None) == {"judge": None}
