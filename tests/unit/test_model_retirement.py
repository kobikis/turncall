"""`gpt-4o-mini` retires on 2027-01-14 (#458).

Every OpenAI agent created before `llm.model`'s default was cleared stores it
(`model_dump()` persisted it), and nothing tells those apart from an agent that
chose it. From that date OpenAI refuses it, and their calls would fail on the
first reply. So: a `model_retiring` warning on every read until then, and from
the date the house model runs instead, logged per call. A dead call is worse
than a different model.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from loguru import logger

from turncall.domain.models import AgentConfig, AnalysisConfig, LLMConfig
from turncall.services.llm_models import resolve_llm_model

pytestmark = pytest.mark.unit

BEFORE = date(2027, 1, 13)
ON = date(2027, 1, 14)
AFTER = date(2027, 6, 1)


class TestResolution:
    def test_before_the_date_it_still_runs(self) -> None:
        assert resolve_llm_model("openai", "gpt-4o-mini", today=BEFORE) == (
            "gpt-4o-mini"
        )

    @pytest.mark.parametrize("today", [ON, AFTER])
    def test_from_the_date_the_house_model_runs(self, today: date) -> None:
        assert resolve_llm_model("openai", "gpt-4o-mini", today=today) == (
            "gpt-5.4-mini"
        )

    def test_each_resolution_after_the_date_is_logged(self) -> None:
        lines: list[str] = []
        sink = logger.add(
            lambda m: lines.append(m), level="WARNING", format="{message}"
        )
        try:
            resolve_llm_model("openai", "gpt-4o-mini", today=ON)
            resolve_llm_model("openai", "gpt-4o-mini", today=BEFORE)
        finally:
            logger.remove(sink)

        assert [line.strip() for line in lines] == [
            "llm_model_retired from=gpt-4o-mini to=gpt-5.4-mini"
        ]

    def test_other_models_and_providers_are_untouched(self) -> None:
        assert resolve_llm_model("openai", "gpt-4.1", today=AFTER) == "gpt-4.1"
        # Off OpenAI it is the old default, read as unset as before.
        assert "claude" in resolve_llm_model("anthropic", "gpt-4o-mini", today=AFTER)


def _warnings(config: AgentConfig) -> list[dict[str, Any]]:
    from turncall.orchestrator.config_warnings import config_warnings

    return [w for w in config_warnings(config) if w["code"] == "model_retiring"]


class TestTheAgentWarning:
    def test_the_agent_llm_is_warned_about(self) -> None:
        [warning] = _warnings(AgentConfig(llm=LLMConfig(model="gpt-4o-mini")))

        assert warning["model"] == "gpt-4o-mini"
        assert warning["retires_on"] == "2027-01-14"
        assert warning["field"] == "llm.model"
        assert "2027-01-14" in warning["message"]
        assert "gpt-5.4-mini" in warning["message"]

    def test_a_failover_entry_and_the_analysis_model_too(self) -> None:
        config = AgentConfig(
            llm=LLMConfig(
                provider="anthropic",
                model="claude-sonnet-5",
                failover=[
                    LLMConfig(provider="openai", model="gpt-5.4-mini"),
                    LLMConfig(provider="openai", model="gpt-4o-mini"),
                ],
            ),
            analysis=AnalysisConfig(model="gpt-4o-mini"),
        )
        # analysis.model rides on the agent's own provider: anthropic here, so
        # `gpt-4o-mini` there is the old default, not a retiring model.
        assert [w["field"] for w in _warnings(config)] == ["llm.failover[1].model"]

        openai = AgentConfig(
            llm=LLMConfig(model="gpt-5.4-mini"),
            analysis=AnalysisConfig(model="gpt-4o-mini"),
        )
        assert [w["field"] for w in _warnings(openai)] == ["analysis.model"]

    @pytest.mark.parametrize(
        "llm",
        [
            LLMConfig(model=""),
            LLMConfig(model="gpt-5.4-mini"),
            LLMConfig(provider="anthropic", model="gpt-4o-mini"),
        ],
    )
    def test_nothing_retiring_says_nothing(self, llm: LLMConfig) -> None:
        assert _warnings(AgentConfig(llm=llm)) == []


class TestTheEvalWarning:
    def _scenario(self, **models: Any) -> Any:
        from turncall.api.v1.schemas.evals import EvalScenarioResponse

        return EvalScenarioResponse.model_validate(
            SimpleNamespace(
                id=uuid4(),
                project_id=uuid4(),
                name="greets",
                description=None,
                kind="script",
                definition={
                    "turns": [
                        {
                            "user": "hi",
                            "expect": [
                                {"event": "llm_response", "text_contains": "hi"}
                            ],
                        }
                    ]
                },
                schema_version="pipecat-1.11",
                tool_mocks={},
                tool_policy="mock_only",
                tags=[],
                default_target=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                **models,
            )
        )

    def test_a_judge_and_a_persona_naming_it_are_warned_about(self) -> None:
        response = self._scenario(
            judge={"provider": "openai", "model": "gpt-4o-mini"},
            simulator={"provider": "openai", "model": "gpt-4o-mini"},
        )

        assert [(w["code"], w["field"]) for w in response.warnings] == [
            ("model_retiring", "judge.model"),
            ("model_retiring", "simulator.model"),
        ]

    def test_another_judge_says_nothing(self) -> None:
        response = self._scenario(
            judge={"provider": "openai", "model": "gpt-5.4-mini"},
            simulator={"provider": "ollama", "model": "gpt-4o-mini"},
        )
        assert response.warnings == []

    def test_the_judge_runs_the_house_model_from_the_date(self, monkeypatch) -> None:
        import pipecat.services.openai.llm as openai_llm

        from turncall.evals import judges
        from turncall.services import llm_models

        seen: dict[str, Any] = {}

        class _Fake:
            Settings = openai_llm.OpenAILLMService.Settings

            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)

        monkeypatch.setattr(openai_llm, "OpenAILLMService", _Fake)
        monkeypatch.setattr(judges, "_platform_key", lambda attr: "sk-test")
        monkeypatch.setattr(llm_models, "_today", lambda: ON)

        judges.openai({"model": "gpt-4o-mini"})

        assert seen["settings"].model == "gpt-5.4-mini"
