"""Jev as a judge: a classifier where every other provider is an LLM (#141).

Verdicts from TypeSafe's classification model in a few hundred milliseconds,
with a calibrated probability rather than a model's own guess at its
confidence. Opt-in and additive — an unconfigured platform keeps the judge it
has.

What is worth testing is what a scenario author can observe: where `jev` is
allowed and where it is not, that a stored scenario naming it survives a
platform with no credential, and that a run on such a platform says which of
the credential and the extra is missing.

Deliberately not tested: that Jev *agrees* with an LLM judge on any scenario.
Two judges disagreeing is data about the scenario, not a defect.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from turncall.api.v1.schemas.evals import (
    CreateEvalScenarioRequest,
    InlineScenario,
    UpdateEvalScenarioRequest,
)
from turncall.domain.enums import EvalModality
from turncall.evals import judges
from turncall.evals.judges import (
    CLASSIFIER_ONLY_JUDGES,
    JUDGE_PROVIDERS,
    PROVIDERS,
    JudgeUnavailable,
)
from turncall.evals.runner import _parse_for_run
from turncall.evals.scenario import ScenarioError, compile_model_block
from turncall.evals.snapshots import harness_config

pytestmark = pytest.mark.unit

SCRIPTED = {
    "turns": [{"user": "hi", "expect": [{"event": "llm_response", "eval": "greets"}]}]
}
JEV_FACTORY = "turncall.evals.judges.jev"


def _settings(**over):
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


def _run(**resolved):
    return SimpleNamespace(
        id=uuid4(),
        scenario_id=uuid4(),
        scenario_name="greets",
        resolved_scenario={"definition": SCRIPTED, **resolved},
    )


class TestJevIsJudgeOnly:
    def test_a_judge_may_be_it(self) -> None:
        assert compile_model_block({"provider": "jev"})["factory"] == JEV_FACTORY

    def test_a_simulator_may_not_and_the_error_names_the_role(self) -> None:
        """A persona is linked into a pipeline to speak; a classifier has no
        `link`. The split allowlist is what makes that structural — no rule of
        its own, and the existing check already names the role."""
        assert "jev" in JUDGE_PROVIDERS
        assert "jev" not in PROVIDERS
        with pytest.raises(ScenarioError, match="unknown simulator provider 'jev'"):
            compile_model_block({"provider": "jev"}, role="simulator")

    @pytest.mark.parametrize(
        "request_type", [CreateEvalScenarioRequest, InlineScenario]
    )
    def test_the_api_refuses_it_in_a_simulator_block(self, request_type) -> None:
        with pytest.raises(ValueError, match="unknown simulator provider"):
            request_type(
                name="wrong block", definition=SCRIPTED, simulator={"provider": "jev"}
            )

    def test_the_platform_setting_refuses_it_for_the_persona(self) -> None:
        """Refused at startup rather than per run: every simulation on the
        platform would otherwise error with nothing naming the cause."""
        from turncall.config.settings import EvalSettings

        assert EvalSettings(judge_provider="jev").judge_provider == "jev"
        with pytest.raises(ValueError, match="EVAL_SIMULATOR_PROVIDER"):
            EvalSettings(simulator_provider="jev")

    def test_an_update_refuses_it_too(self) -> None:
        with pytest.raises(ValueError, match="unknown simulator provider"):
            UpdateEvalScenarioRequest(simulator={"provider": "jev"})

    def test_a_judge_block_is_accepted_on_the_way_in(self) -> None:
        body = CreateEvalScenarioRequest(
            name="fast judge", definition=SCRIPTED, judge={"provider": "jev"}
        )
        assert body.judge.provider == "jev"

    def test_a_key_is_still_refused_in_the_block(self) -> None:
        """The credential is the platform's: one settable here would be stored
        in JSONB, returned by a read and masked forever after (#91)."""
        from turncall.api.v1.schemas.evals import EvalModelSchema

        with pytest.raises(ValueError, match="api_key"):
            EvalModelSchema(provider="jev", api_key="ts-leaked")

    def test_it_takes_no_temperature(self) -> None:
        """A classifier is not a sampler, and a recorded temperature would
        describe a request nobody made."""
        compiled = compile_model_block({"provider": "jev", "temperature": 0.3})
        assert "extra" not in compiled


class TestItBringsItsOwnExplainer:
    """Jev writes no prose. Pipecat's fallback for a classifier with no
    explainer is a local Ollama, which is the trap #139 closed."""

    def test_the_platform_judge_explains(self) -> None:
        _kind, parsed = _parse_for_run(
            _run(judge={"provider": "jev"}),
            EvalModality.TEXT,
            _settings(judge_provider="openai", judge_model="gpt-4o-mini"),
        )
        assert parsed.judge["factory"] == JEV_FACTORY
        assert parsed.judge["explainer"]["factory"] == "turncall.evals.judges.openai"

        snapshot = harness_config(parsed)
        assert snapshot["judge_provider"] == "jev"
        assert snapshot["explainer_provider"] == "openai"
        assert snapshot["explainer_model"] == "gpt-4o-mini"

    def test_an_ollama_explainer_is_the_plain_llm_not_the_judges_wrapper(self) -> None:
        """An explainer is asked for prose through `llm_service_from_config`, so
        it has to be an LLM service. TurnCall's `ollama` judge factory returns an
        `LLMClassifier` for its wider timeout, and that has no `run_inference`
        contract the explainer can use."""
        _kind, parsed = _parse_for_run(
            _run(judge={"provider": "jev"}),
            EvalModality.TEXT,
            _settings(judge_provider="ollama", judge_model="gemma4:12b"),
        )
        assert parsed.judge["explainer"]["factory"] == "turncall.evals.judges.ollama"

    def test_with_no_platform_judge_reasons_are_off_not_broken(self) -> None:
        """Explicitly off, rather than left to resolve into a service nobody
        configured: probabilities and no prose beats a run that fails."""
        _kind, parsed = _parse_for_run(
            _run(judge={"provider": "jev"}), EvalModality.TEXT, _settings()
        )
        assert parsed.judge["explainer"] is False
        assert harness_config(parsed)["explainer_used"] is False

    def test_a_raw_classifier_block_records_pipecats_fallback(self) -> None:
        """A raw `judge.eval:` wins by design, explainer included — and pipecat
        explains a classifier that named none with its own default LLM. Recording
        the judge there would claim a classifier wrote prose it cannot write."""
        snapshot = harness_config(SimpleNamespace(judge={"factory": JEV_FACTORY}))
        assert snapshot["judge_provider"] == "jev"
        assert snapshot["explainer_used"] is True
        assert snapshot["explainer_provider"] == "ollama"

    def test_jev_never_explains_itself(self) -> None:
        _kind, parsed = _parse_for_run(
            _run(judge={"provider": "jev"}),
            EvalModality.TEXT,
            _settings(judge_provider="jev"),
        )
        assert parsed.judge["explainer"] is False

    def test_the_platform_can_name_it_for_every_scenario(self) -> None:
        _kind, parsed = _parse_for_run(
            _run(), EvalModality.TEXT, _settings(judge_provider="jev")
        )
        assert parsed.judge["factory"] == JEV_FACTORY

    def test_an_llm_judge_is_left_exactly_as_it_was(self) -> None:
        """The whole point of the split: adopting jev changes nothing for the
        judges that already work."""
        assert CLASSIFIER_ONLY_JUDGES == {"jev"}
        _kind, parsed = _parse_for_run(
            _run(), EvalModality.TEXT, _settings(judge_provider="anthropic")
        )
        assert "explainer" not in parsed.judge


class TestSwitchingToItIsAnnounced:
    @pytest.mark.asyncio
    async def test_the_first_run_after_the_switch_says_the_judge_moved(self) -> None:
        """A suite that goes red on the day someone switched judges has to say
        so, or the switch reads as a regression."""
        from unittest.mock import AsyncMock, patch

        from turncall.evals.run_warnings import warn_judge_changed

        _kind, parsed = _parse_for_run(
            _run(judge={"provider": "jev"}), EvalModality.TEXT, _settings()
        )
        previous = {"judge_provider": "ollama", "judge_model": "gemma4:12b"}
        with patch(
            "turncall.storage.repositories.eval_repo.last_judged_harness",
            AsyncMock(return_value=previous),
        ):
            warnings = await warn_judge_changed(
                AsyncMock(), _run(), harness_config(parsed)
            )

        assert [w["code"] for w in warnings] == ["judge_changed"]
        assert warnings[0]["judge"]["judge_provider"]["current"] == "jev"


class TestTheDeploymentDecidesAtRunTime:
    """A credential and an installed extra are properties of a deployment, and a
    stored scenario outlives the deployment it was written against."""

    def test_a_scenario_stores_fine_without_a_credential(self, monkeypatch) -> None:
        monkeypatch.setattr(judges, "_platform_key", lambda attr: "")
        body = CreateEvalScenarioRequest(
            name="written first", definition=SCRIPTED, judge={"provider": "jev"}
        )
        # And it still compiles to a run's block: nothing touches the key until
        # the worker builds the judge.
        assert body.judge.provider == "jev"
        assert compile_model_block({"provider": "jev"})["factory"] == JEV_FACTORY

    def test_a_missing_credential_says_so(self, monkeypatch) -> None:
        monkeypatch.setattr(judges, "_platform_key", lambda attr: "")
        with pytest.raises(JudgeUnavailable, match="TYPESAFE_API_KEY"):
            judges.jev({})

    def test_a_missing_extra_says_so_instead(self, monkeypatch) -> None:
        """Two different fixes, so two different messages."""
        import builtins

        real_import = builtins.__import__

        def _no_jev(name, *args, **kwargs):
            if name.startswith("pipecat.classifiers.jev"):
                raise ImportError("No module named 'pipecat.classifiers.jev'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_jev)
        monkeypatch.setattr(judges, "_platform_key", lambda attr: "ts-test")
        with pytest.raises(JudgeUnavailable, match="evals-jev"):
            judges.jev({})

    def test_the_extra_is_http2_and_that_failure_is_caught_too(
        self, monkeypatch
    ) -> None:
        """`pipecat-ai[jev]` is `httpx[http2]`, and `JevClient` opens its pool
        with `http2=True` — so the extra's real absence is httpx raising from
        *inside* the constructor. Guarding only the import left a raw
        `ImportError: ... the 'h2' package is not installed`."""
        import pipecat.classifiers.jev.classifier as jev_mod

        def _no_h2(**_kwargs):
            raise ImportError(
                "Using http2=True, but the 'h2' package is not installed."
            )

        monkeypatch.setattr(jev_mod, "JevClassifier", _no_h2)
        monkeypatch.setattr(judges, "_platform_key", lambda attr: "ts-test")
        with pytest.raises(JudgeUnavailable, match="evals-jev") as caught:
            judges.jev({})
        assert "h2" in str(caught.value)

    def test_the_key_comes_from_the_platform(self, monkeypatch) -> None:
        import pipecat.classifiers.jev.classifier as jev_mod

        seen: dict = {}
        monkeypatch.setattr(jev_mod, "JevClassifier", lambda **kw: seen.update(kw))
        monkeypatch.setattr(judges, "_platform_key", lambda attr: f"key-for-{attr}")
        judges.jev({"model": "jev-1.13.0", "endpoint": "https://gateway.test"})

        assert seen == {
            "api_key": "key-for-typesafe",
            "model": "jev-1.13.0",
            "base_url": "https://gateway.test",
        }

    def test_jevs_own_defaults_are_left_alone(self, monkeypatch) -> None:
        """Pipecat pins the model so thresholds tuned against it hold."""
        import pipecat.classifiers.jev.classifier as jev_mod

        seen: dict = {}
        monkeypatch.setattr(jev_mod, "JevClassifier", lambda **kw: seen.update(kw))
        monkeypatch.setattr(judges, "_platform_key", lambda attr: "ts-test")
        judges.jev({})

        assert seen == {"api_key": "ts-test"}
