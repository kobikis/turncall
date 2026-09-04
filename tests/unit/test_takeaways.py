"""Takeaways (ADR-0013): extraction validation/retry + API schema rules."""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.takeaways import CreateTakeawayRequest
from turncall.domain.models import LLMConfig
from turncall.services.call_analysis import extract_takeaway

CSAT_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string"},
    },
    "required": ["score"],
}


class _Completion:
    def __init__(self, text: str) -> None:
        self.text = text


@pytest.mark.unit
class TestCreateTakeawayRequest:
    def test_valid(self) -> None:
        req = CreateTakeawayRequest(name="csat_score", schema=CSAT_SCHEMA)
        assert req.schema_ == CSAT_SCHEMA

    def test_name_must_be_identifier_shaped(self) -> None:
        with pytest.raises(ValidationError):
            CreateTakeawayRequest(name="CSAT Score!", schema=CSAT_SCHEMA)

    def test_rejects_invalid_json_schema(self) -> None:
        with pytest.raises(ValidationError, match="not a valid JSON Schema"):
            CreateTakeawayRequest(
                name="bad", schema={"type": "object", "properties": "nope"}
            )


@pytest.mark.unit
@pytest.mark.asyncio
class TestExtractTakeaway:
    async def test_valid_extraction(self) -> None:
        with patch(
            "turncall.services.call_analysis.complete_text",
            new=AsyncMock(return_value=_Completion('{"score": 4, "reason": "happy"}')),
        ):
            out = await extract_takeaway(
                "Customer: great service!",
                name="csat_score",
                schema=CSAT_SCHEMA,
                llm_config=LLMConfig(),
            )
        assert out["valid"] is True
        assert out["result"]["score"] == 4

    async def test_retry_fixes_invalid_then_succeeds(self) -> None:
        mock = AsyncMock(
            side_effect=[_Completion('{"score": 9}'), _Completion('{"score": 5}')]
        )
        with patch("turncall.services.call_analysis.complete_text", new=mock):
            out = await extract_takeaway(
                "t", name="csat_score", schema=CSAT_SCHEMA, llm_config=LLMConfig()
            )
        assert mock.await_count == 2  # retried with the validation error
        assert out["valid"] is True
        assert out["result"]["score"] == 5

    async def test_invalid_after_retry_reports_not_raises(self) -> None:
        mock = AsyncMock(return_value=_Completion("not json at all"))
        with patch("turncall.services.call_analysis.complete_text", new=mock):
            out = await extract_takeaway(
                "t", name="csat_score", schema=CSAT_SCHEMA, llm_config=LLMConfig()
            )
        assert out["valid"] is False
        assert out["result"] is None
        assert out["error"]

    async def test_llm_error_reports_not_raises(self) -> None:
        with patch(
            "turncall.services.call_analysis.complete_text",
            new=AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            out = await extract_takeaway(
                "t", name="csat_score", schema=CSAT_SCHEMA, llm_config=LLMConfig()
            )
        assert out["valid"] is False
        assert "provider down" in out["error"]

    async def test_strips_markdown_fences(self) -> None:
        with patch(
            "turncall.services.call_analysis.complete_text",
            new=AsyncMock(return_value=_Completion('```json\n{"score": 3}\n```')),
        ):
            out = await extract_takeaway(
                "t", name="csat_score", schema=CSAT_SCHEMA, llm_config=LLMConfig()
            )
        assert out["valid"] is True
        assert out["result"] == {"score": 3}
