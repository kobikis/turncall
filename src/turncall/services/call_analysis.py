"""Post-call structured analysis service.

Runs after the call ends (~2-5s) and its results are delivered inline in the
single `call.ended` webhook — there is no separate `analysis.completed` event.

The analysis uses the agent's LLM config (or an override model) to
produce: summary, success evaluation, sentiment, structured extraction,
and scoring rubric results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from turncall.domain.models import AnalysisConfig, AWSConfig, LLMConfig
from turncall.services.llm_text import complete_text


@dataclass(frozen=True)
class AnalysisResult:
    """Immutable result of post-call analysis."""

    summary: str | None = None
    success_evaluation: dict[str, Any] | None = None
    sentiment: dict[str, Any] | None = None
    structured_data: dict[str, Any] | None = None
    scoring: dict[str, Any] | None = None
    takeaways: dict[str, Any] | None = None  # {name: {result, valid, ...}} ADR-0013
    model: str = ""
    duration_ms: int = 0
    analyzed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "model": self.model,
            "duration_ms": self.duration_ms,
            "analyzed_at": self.analyzed_at,
        }
        if self.summary is not None:
            result["summary"] = self.summary
        if self.success_evaluation is not None:
            result["success_evaluation"] = self.success_evaluation
        if self.sentiment is not None:
            result["sentiment"] = self.sentiment
        if self.structured_data is not None:
            result["structured_data"] = self.structured_data
        if self.scoring is not None:
            result["scoring"] = self.scoring
        if self.takeaways is not None:
            result["takeaways"] = self.takeaways
        return result


def _build_transcript_text(events: list[dict[str, Any]]) -> str:
    """Build a readable transcript from call events."""
    lines: list[str] = []
    for event in events:
        text = event.get("text", "")
        user_id = event.get("user_id", "unknown")
        if not text:
            continue
        role = "Agent" if user_id == "assistant" else "Customer"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _build_analysis_prompt(
    transcript: str,
    analysis_config: AnalysisConfig,
    system_prompt: str,
) -> list[dict[str, str]]:
    """Build the LLM messages for analysis."""
    sections: list[str] = []
    sections.append(
        "You are an expert call analyst. Analyze the following voice call transcript "
        "and return a JSON object with the requested sections. Return ONLY valid JSON, "
        "no markdown fences."
    )

    response_schema: dict[str, str] = {}

    if analysis_config.summary_enabled:
        if analysis_config.summary_prompt:
            sections.append(
                f"\n## Summary\n{analysis_config.summary_prompt}\n"
                'Include a "summary" key (string) in your response.'
            )
        else:
            sections.append(
                "\n## Summary\n"
                "Provide a concise summary of the call covering: purpose, key topics "
                "discussed, outcomes, and any action items.\n"
                'Include a "summary" key (string) in your response.'
            )
        response_schema["summary"] = "string"

    eval_cfg = analysis_config.success_evaluation
    if eval_cfg.enabled:
        scale_desc = {
            "pass_fail": '"pass" or "fail"',
            "likert": "an integer from 1 to 5",
            "numeric": "an integer from 0 to 100",
        }.get(eval_cfg.scale, '"pass" or "fail"')

        rubric_text = (
            eval_cfg.rubric
            or "Evaluate whether the call achieved its intended purpose."
        )
        sections.append(
            f"\n## Success Evaluation\n"
            f"Rubric: {rubric_text}\n"
            f'Return a "success_evaluation" object with:\n'
            f'  - "score": {scale_desc}\n'
            f'  - "reason": brief explanation'
        )
        response_schema["success_evaluation"] = '{"score": ..., "reason": "..."}'

    if analysis_config.sentiment_enabled:
        sections.append(
            "\n## Sentiment\n"
            "Analyze the overall customer sentiment.\n"
            'Return a "sentiment" object with:\n'
            '  - "overall": one of "positive", "neutral", "negative", "mixed"\n'
            '  - "customer_satisfaction": one of "satisfied", "neutral", "dissatisfied"\n'
            '  - "reason": brief explanation'
        )
        response_schema["sentiment"] = (
            '{"overall": "...", "customer_satisfaction": "...", "reason": "..."}'
        )

    if analysis_config.structured_extraction_schema:
        schema_str = json.dumps(analysis_config.structured_extraction_schema, indent=2)
        sections.append(
            f"\n## Structured Extraction\n"
            f"Extract data from the transcript according to this JSON Schema:\n"
            f"```\n{schema_str}\n```\n"
            f'Return the extracted data under a "structured_data" key.'
        )
        response_schema["structured_data"] = "object matching schema"

    if analysis_config.scoring_rubric:
        rubric_str = json.dumps(analysis_config.scoring_rubric, indent=2)
        sections.append(
            f"\n## Scoring Rubric\n"
            f"Score the call on these criteria:\n"
            f"```\n{rubric_str}\n```\n"
            f'Return scores under a "scoring" key as an object with each criterion '
            f'name mapped to {{"score": number, "reason": "..."}}.'
        )
        response_schema["scoring"] = "{criterion: {score, reason}}"

    system_text = "\n".join(sections)
    if system_prompt:
        system_text += (
            f"\n\n## Agent Context\nThe agent's system prompt was:\n"
            f"{system_prompt[:2000]}"
        )

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": f"## Call Transcript\n\n{transcript}"},
    ]


def _parse_analysis_response(
    text: str,
    analysis_config: AnalysisConfig,
) -> dict[str, Any]:
    """Parse the LLM JSON response into structured sections."""
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last fence lines
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("analysis_json_parse_failed", raw_length=len(text))
        return {"summary": text, "_parse_error": True}

    result: dict[str, Any] = {}

    if analysis_config.summary_enabled and "summary" in data:
        result["summary"] = data["summary"]

    if analysis_config.success_evaluation.enabled and "success_evaluation" in data:
        result["success_evaluation"] = data["success_evaluation"]

    if analysis_config.sentiment_enabled and "sentiment" in data:
        result["sentiment"] = data["sentiment"]

    if analysis_config.structured_extraction_schema and "structured_data" in data:
        result["structured_data"] = data["structured_data"]

    if analysis_config.scoring_rubric and "scoring" in data:
        result["scoring"] = data["scoring"]

    return result


async def analyze_call(
    transcript_events: list[dict[str, Any]],
    analysis_config: AnalysisConfig,
    llm_config: LLMConfig,
    *,
    system_prompt: str = "",
    aws: AWSConfig | None = None,
) -> AnalysisResult:
    """Run post-call analysis on a transcript.

    Args:
        transcript_events: List of transcript event payloads (text + user_id).
        analysis_config: Analysis configuration from agent config.
        llm_config: Agent's LLM config (model may be overridden by analysis_config.model).
        system_prompt: The agent's system prompt for context.

    Returns:
        Frozen AnalysisResult with all requested sections.
    """
    if not analysis_config.enabled:
        return AnalysisResult(analyzed_at=datetime.now(UTC).isoformat())

    transcript = _build_transcript_text(transcript_events)
    if not transcript.strip():
        logger.info("analysis_skipped_empty_transcript")
        return AnalysisResult(
            summary="No transcript available for analysis.",
            analyzed_at=datetime.now(UTC).isoformat(),
        )

    # Use override model if specified
    effective_llm = llm_config
    if analysis_config.model:
        effective_llm = LLMConfig(
            provider=llm_config.provider,
            model=analysis_config.model,
            temperature=0.3,  # Lower temperature for analysis
            max_tokens=4096,
            base_url=llm_config.base_url,
            api_key=llm_config.api_key,
            extra=llm_config.extra,
        )

    messages = _build_analysis_prompt(transcript, analysis_config, system_prompt)

    start = datetime.now(UTC)
    try:
        completion = await complete_text(effective_llm, messages, aws=aws)
    except Exception:
        logger.exception("analysis_llm_error")
        return AnalysisResult(
            summary="Analysis failed: LLM error",
            model=effective_llm.model,
            analyzed_at=start.isoformat(),
        )
    elapsed_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)

    parsed = _parse_analysis_response(completion.text, analysis_config)

    return AnalysisResult(
        summary=parsed.get("summary"),
        success_evaluation=parsed.get("success_evaluation"),
        sentiment=parsed.get("sentiment"),
        structured_data=parsed.get("structured_data"),
        scoring=parsed.get("scoring"),
        model=effective_llm.model,
        duration_ms=elapsed_ms,
        analyzed_at=start.isoformat(),
    )


# ---------------------------------------------------------- takeaways (ADR-0013)


_TAKEAWAY_SYSTEM = (
    "You extract structured data from a finished phone conversation. "
    "Respond with a single JSON object matching the provided JSON Schema exactly — "
    "no prose, no markdown fences. Use null for information that was not mentioned "
    "unless the schema requires the field."
)


def _strip_json_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.split("\n") if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines)
    return cleaned.strip()


async def extract_takeaway(
    transcript: str,
    *,
    name: str,
    schema: dict[str, Any],
    llm_config: LLMConfig,
    description: str | None = None,
    prompt: str | None = None,
    model: str | None = None,
    system_prompt: str = "",
    aws: AWSConfig | None = None,
) -> dict[str, Any]:
    """Run one takeaway extraction: LLM call, schema validation, one retry.

    Never raises — an invalid or failed extraction is reported with
    valid=False so one bad takeaway can't sink the others (they run
    concurrently via asyncio.gather in the trigger).
    """
    import jsonschema

    effective_llm = llm_config
    if model:
        effective_llm = LLMConfig(
            provider=llm_config.provider,
            model=model,
            temperature=0.2,
            max_tokens=2048,
            base_url=llm_config.base_url,
            api_key=llm_config.api_key,
            extra=llm_config.extra,
        )

    parts = [f"Takeaway: {name}"]
    if description:
        parts.append(f"Purpose: {description}")
    if prompt:
        parts.append(f"Instructions: {prompt}")
    if system_prompt:
        parts.append(f"The agent's role, for context:\n{system_prompt[:1000]}")
    parts.append(f"JSON Schema:\n{json.dumps(schema, indent=2)}")
    parts.append(f"Conversation transcript:\n{transcript}")
    user_msg = "\n\n".join(parts)

    start = datetime.now(UTC)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _TAKEAWAY_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    last_error = ""
    for _attempt in range(2):  # initial + one retry with the validation error
        try:
            completion = await complete_text(effective_llm, messages, aws=aws)
            data = json.loads(_strip_json_fences(completion.text))
            jsonschema.validate(data, schema)
            return {
                "result": data,
                "valid": True,
                "model": effective_llm.model,
                "duration_ms": int(
                    (datetime.now(UTC) - start).total_seconds() * 1000
                ),
            }
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last_error = str(exc)[:500]
            messages = [
                *messages,
                {"role": "assistant", "content": completion.text},
                {
                    "role": "user",
                    "content": "That response failed validation: "
                    f"{last_error}\nRespond again with ONLY a corrected JSON object.",
                },
            ]
        except Exception as exc:  # LLM/transport error — no point retrying blind
            last_error = str(exc)[:500]
            break

    logger.warning("takeaway_extraction_failed", takeaway=name, error=last_error)
    return {
        "result": None,
        "valid": False,
        "error": last_error,
        "model": effective_llm.model,
        "duration_ms": int((datetime.now(UTC) - start).total_seconds() * 1000),
    }
