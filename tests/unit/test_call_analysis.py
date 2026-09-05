"""Tests for post-call structured analysis."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.domain.models import AnalysisConfig, LLMConfig, SuccessEvaluationConfig
from turncall.services.call_analysis import (
    AnalysisResult,
    _build_analysis_prompt,
    _build_transcript_text,
    _parse_analysis_response,
    analyze_call,
)


@pytest.mark.unit
class TestBuildTranscriptText:
    def test_basic_transcript(self) -> None:
        events = [
            {"text": "Hello, how can I help?", "user_id": "assistant"},
            {"text": "I need to book an appointment", "user_id": "customer"},
            {"text": "Sure, what time works?", "user_id": "assistant"},
        ]
        result = _build_transcript_text(events)
        assert "Agent: Hello, how can I help?" in result
        assert "Customer: I need to book an appointment" in result
        assert "Agent: Sure, what time works?" in result

    def test_empty_events(self) -> None:
        assert _build_transcript_text([]) == ""

    def test_skips_empty_text(self) -> None:
        events = [
            {"text": "Hello", "user_id": "assistant"},
            {"text": "", "user_id": "customer"},
            {"text": "Goodbye", "user_id": "assistant"},
        ]
        result = _build_transcript_text(events)
        lines = [line for line in result.split("\n") if line.strip()]
        assert len(lines) == 2

    def test_unknown_user_id(self) -> None:
        events = [{"text": "Hi", "user_id": "unknown"}]
        result = _build_transcript_text(events)
        assert result == "Customer: Hi"


@pytest.mark.unit
class TestBuildAnalysisPrompt:
    def test_summary_only(self) -> None:
        config = AnalysisConfig(summary_enabled=True)
        messages = _build_analysis_prompt("Agent: Hi\nCustomer: Hello", config, "")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "summary" in messages[0]["content"].lower()
        assert messages[1]["role"] == "user"
        assert "Agent: Hi" in messages[1]["content"]

    def test_custom_summary_prompt(self) -> None:
        config = AnalysisConfig(
            summary_enabled=True,
            summary_prompt="Focus on customer satisfaction.",
        )
        messages = _build_analysis_prompt("transcript", config, "")
        assert "Focus on customer satisfaction" in messages[0]["content"]

    def test_success_evaluation(self) -> None:
        config = AnalysisConfig(
            summary_enabled=False,
            success_evaluation=SuccessEvaluationConfig(
                enabled=True,
                rubric="Did the agent resolve the issue?",
                scale="pass_fail",
            ),
        )
        messages = _build_analysis_prompt("transcript", config, "")
        system = messages[0]["content"]
        assert "success_evaluation" in system
        assert "Did the agent resolve the issue?" in system
        assert '"pass" or "fail"' in system

    def test_likert_scale(self) -> None:
        config = AnalysisConfig(
            summary_enabled=False,
            success_evaluation=SuccessEvaluationConfig(enabled=True, scale="likert"),
        )
        messages = _build_analysis_prompt("transcript", config, "")
        assert "1 to 5" in messages[0]["content"]

    def test_sentiment(self) -> None:
        config = AnalysisConfig(summary_enabled=False, sentiment_enabled=True)
        messages = _build_analysis_prompt("transcript", config, "")
        assert "sentiment" in messages[0]["content"].lower()

    def test_structured_extraction(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "appointment_date": {"type": "string"},
                "customer_name": {"type": "string"},
            },
        }
        config = AnalysisConfig(
            summary_enabled=False, structured_extraction_schema=schema
        )
        messages = _build_analysis_prompt("transcript", config, "")
        assert "structured_data" in messages[0]["content"]
        assert "appointment_date" in messages[0]["content"]

    def test_scoring_rubric(self) -> None:
        rubric = {
            "professionalism": {
                "max_score": 10,
                "description": "Was the agent professional?",
            },
            "resolution": {"max_score": 10, "description": "Was the issue resolved?"},
        }
        config = AnalysisConfig(summary_enabled=False, scoring_rubric=rubric)
        messages = _build_analysis_prompt("transcript", config, "")
        assert "scoring" in messages[0]["content"]
        assert "professionalism" in messages[0]["content"]

    def test_system_prompt_appended(self) -> None:
        config = AnalysisConfig(summary_enabled=True)
        messages = _build_analysis_prompt(
            "transcript", config, "You are a receptionist."
        )
        assert "You are a receptionist" in messages[0]["content"]

    def test_all_sections(self) -> None:
        config = AnalysisConfig(
            summary_enabled=True,
            success_evaluation=SuccessEvaluationConfig(enabled=True, scale="numeric"),
            sentiment_enabled=True,
            structured_extraction_schema={"type": "object", "properties": {}},
            scoring_rubric={"quality": {"max_score": 10}},
        )
        messages = _build_analysis_prompt("transcript", config, "sys prompt")
        system = messages[0]["content"]
        assert "summary" in system.lower()
        assert "success_evaluation" in system
        assert "sentiment" in system.lower()
        assert "structured_data" in system
        assert "scoring" in system


@pytest.mark.unit
class TestParseAnalysisResponse:
    def test_parse_valid_json(self) -> None:
        config = AnalysisConfig(
            summary_enabled=True,
            sentiment_enabled=True,
        )
        response = json.dumps(
            {
                "summary": "Customer called about billing.",
                "sentiment": {
                    "overall": "neutral",
                    "customer_satisfaction": "neutral",
                    "reason": "Standard inquiry",
                },
            }
        )
        result = _parse_analysis_response(response, config)
        assert result["summary"] == "Customer called about billing."
        assert result["sentiment"]["overall"] == "neutral"

    def test_parse_with_code_fences(self) -> None:
        config = AnalysisConfig(summary_enabled=True)
        response = '```json\n{"summary": "Test summary"}\n```'
        result = _parse_analysis_response(response, config)
        assert result["summary"] == "Test summary"

    def test_parse_invalid_json(self) -> None:
        config = AnalysisConfig(summary_enabled=True)
        result = _parse_analysis_response("not json at all", config)
        assert result.get("_parse_error") is True
        assert "summary" in result

    def test_only_returns_configured_sections(self) -> None:
        config = AnalysisConfig(
            summary_enabled=True,
            sentiment_enabled=False,
        )
        response = json.dumps(
            {
                "summary": "A summary",
                "sentiment": {"overall": "positive"},
            }
        )
        result = _parse_analysis_response(response, config)
        assert "summary" in result
        assert "sentiment" not in result

    def test_success_evaluation_parsed(self) -> None:
        config = AnalysisConfig(
            summary_enabled=False,
            success_evaluation=SuccessEvaluationConfig(enabled=True, scale="pass_fail"),
        )
        response = json.dumps(
            {
                "success_evaluation": {"score": "pass", "reason": "Issue resolved"},
            }
        )
        result = _parse_analysis_response(response, config)
        assert result["success_evaluation"]["score"] == "pass"

    def test_structured_data_parsed(self) -> None:
        config = AnalysisConfig(
            summary_enabled=False,
            structured_extraction_schema={"type": "object"},
        )
        response = json.dumps(
            {
                "structured_data": {"appointment_date": "2026-04-20", "name": "Jane"},
            }
        )
        result = _parse_analysis_response(response, config)
        assert result["structured_data"]["name"] == "Jane"


@pytest.mark.unit
class TestAnalysisResult:
    def test_to_dict_minimal(self) -> None:
        result = AnalysisResult(model="gpt-4o-mini", analyzed_at="2026-04-17T10:00:00Z")
        d = result.to_dict()
        assert d["model"] == "gpt-4o-mini"
        assert "summary" not in d
        assert "sentiment" not in d

    def test_to_dict_full(self) -> None:
        result = AnalysisResult(
            summary="Test summary",
            success_evaluation={"score": "pass", "reason": "OK"},
            sentiment={"overall": "positive"},
            structured_data={"key": "value"},
            scoring={"quality": {"score": 8, "reason": "Good"}},
            model="gpt-4o",
            duration_ms=1500,
            analyzed_at="2026-04-17T10:00:00Z",
        )
        d = result.to_dict()
        assert d["summary"] == "Test summary"
        assert d["success_evaluation"]["score"] == "pass"
        assert d["sentiment"]["overall"] == "positive"
        assert d["structured_data"]["key"] == "value"
        assert d["scoring"]["quality"]["score"] == 8

    def test_frozen(self) -> None:
        result = AnalysisResult(summary="test")
        with pytest.raises(AttributeError):
            result.summary = "changed"  # type: ignore[misc]


@pytest.mark.unit
class TestAnalyzeCall:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self) -> None:
        config = AnalysisConfig(enabled=False)
        llm = LLMConfig(api_key="test")
        result = await analyze_call([], config, llm)
        assert result.summary is None

    @pytest.mark.asyncio
    async def test_empty_transcript(self) -> None:
        config = AnalysisConfig(enabled=True, summary_enabled=True)
        llm = LLMConfig(api_key="test")
        result = await analyze_call([], config, llm)
        assert "No transcript" in (result.summary or "")

    @pytest.mark.asyncio
    async def test_successful_analysis(self) -> None:
        config = AnalysisConfig(
            enabled=True,
            summary_enabled=True,
            sentiment_enabled=True,
        )
        llm = LLMConfig(provider="openai", model="gpt-4o-mini", api_key="test")
        events = [
            {"text": "Hello, how can I help?", "user_id": "assistant"},
            {"text": "I need help with billing", "user_id": "customer"},
        ]

        mock_result = MagicMock()
        mock_result.text = json.dumps(
            {
                "summary": "Customer called about billing.",
                "sentiment": {
                    "overall": "neutral",
                    "customer_satisfaction": "neutral",
                    "reason": "Standard inquiry",
                },
            }
        )
        mock_result.total_tokens = 100

        with patch(
            "turncall.services.call_analysis.complete_text",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await analyze_call(events, config, llm)

        assert result.summary == "Customer called about billing."
        assert result.sentiment is not None
        assert result.sentiment["overall"] == "neutral"
        assert result.model == "gpt-4o-mini"
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_model_override(self) -> None:
        config = AnalysisConfig(
            enabled=True,
            summary_enabled=True,
            model="gpt-4o",
        )
        llm = LLMConfig(provider="openai", model="gpt-4o-mini", api_key="test")
        events = [{"text": "Hi", "user_id": "assistant"}]

        captured_config = None

        async def capture_complete_text(
            config: LLMConfig, messages: list, **kwargs: object
        ) -> MagicMock:
            nonlocal captured_config
            captured_config = config
            result = MagicMock()
            result.text = json.dumps({"summary": "Test"})
            result.total_tokens = 50
            return result

        with patch(
            "turncall.services.call_analysis.complete_text",
            side_effect=capture_complete_text,
        ):
            result = await analyze_call(events, config, llm)

        assert captured_config is not None
        assert captured_config.model == "gpt-4o"
        assert result.model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_llm_error_returns_error_result(self) -> None:
        config = AnalysisConfig(enabled=True, summary_enabled=True)
        llm = LLMConfig(provider="openai", api_key="test")
        events = [{"text": "Hi", "user_id": "assistant"}]

        with patch(
            "turncall.services.call_analysis.complete_text",
            new_callable=AsyncMock,
            side_effect=Exception("LLM down"),
        ):
            result = await analyze_call(events, config, llm)

        assert "failed" in (result.summary or "").lower()


@pytest.mark.unit
class TestAnalysisTrigger:
    def test_trigger_always_returns_task(self) -> None:
        import asyncio
        import uuid

        from turncall.services.call_analysis_trigger import trigger_post_call_analysis

        async def run() -> None:
            factory = MagicMock()
            # Even with analysis disabled, trigger returns a task
            # (still gathers transcript + recording for call.ended webhook)
            task = trigger_post_call_analysis(
                factory,
                uuid.uuid4(),
                uuid.uuid4(),
                {"analysis": {"enabled": False}},
            )
            assert isinstance(task, asyncio.Task)
            task.cancel()

        asyncio.run(run())

    def test_trigger_with_analysis_enabled(self) -> None:
        import asyncio
        import uuid

        from turncall.services.call_analysis_trigger import trigger_post_call_analysis

        async def run() -> None:
            factory = MagicMock()
            task = trigger_post_call_analysis(
                factory,
                uuid.uuid4(),
                uuid.uuid4(),
                {"analysis": {"enabled": True, "summary_enabled": True}},
            )
            assert isinstance(task, asyncio.Task)
            task.cancel()

        asyncio.run(run())

    def test_trigger_no_analysis_key_defaults_enabled(self) -> None:
        import asyncio
        import uuid

        from turncall.services.call_analysis_trigger import trigger_post_call_analysis

        async def run() -> None:
            factory = MagicMock()
            task = trigger_post_call_analysis(
                factory,
                uuid.uuid4(),
                uuid.uuid4(),
                {},  # No analysis key — defaults to enabled
            )
            assert task is not None
            task.cancel()

        asyncio.run(run())
