"""Responses-over-WebSocket against Chat Completions, measured (#411).

pipecat's `OpenAIResponsesLLMService` keeps one WebSocket per call and sends
only the items added since the previous response. Whether that answers sooner
on TurnCall's own pipeline is #411's gate: median LLM latency at least 15%
lower from turn 5 on, p95 no worse. Turns 1-4 are reported but not judged — the
context is short there, and that is where the two should differ least.

The latency is timed here, not read from pipecat's TTFB: from the LLM's
`LLMFullResponseStartFrame` to its first non-empty `LLMTextFrame`, on the
pipeline clock, the same way for both services. pipecat 1.14's Responses
service stops its TTFB timer on `response.output_item.added` and on a
`previous_response_not_found` error before retrying with the full context, so
its first report can land before any text exists — 0.17 s against a real
0.67 s in the first smoke run. A response cancelled before any text (the caller
talked over it) has no answer and is counted apart.

Same agent, same model, one scripted 10-turn conversation, audio modality over
the eval loopback. Arms alternate A, B, A, B so a slow stretch on OpenAI's side
lands on both. Token usage is collected beside it: the bill is the other half
of #411.

A second test runs a reasoning model with a tool on Responses (#411 again):
Chat Completions refuses tools plus a reasoning effort from gpt-5.4 on, so for
those models Responses is the only way to keep both. It times the same first
text at no reasoning and at `low`, so the thinking time is a number.

Opt-in: costs ~20 real conversations. `RESPONSES_LATENCY_ROUNDS=10 pytest -m
live tests/live/test_live_openai_responses_latency.py -s`. The result is also
written as JSON to `RESPONSES_LATENCY_OUT` when set. The reasoning test is
`RESPONSES_REASONING_ROUNDS=3`, a few cents. Reports, never asserts the
gate: a measurement that fails on a slow afternoon is noise, not a regression.
"""

import json
import os
import statistics
from typing import Any
from uuid import uuid4

import pytest
from loguru import logger
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.llm_service import LLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.orchestrator import pipeline_factory, turn_metrics
from turncall.services.tool_mocks import ToolMocks

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_MODEL = "gpt-4.1"
_JUDGED_FROM_TURN = 5
# Names Kokoro speaks and Nova-3 hears reliably: "Italy" came back "Erle".
_CAPITALS = [
    ("France", "Paris"),
    ("Germany", "Berlin"),
    ("Spain", "Madrid"),
    ("Canada", "Ottawa"),
    ("Mexico", "Mexico City"),
    ("England", "London"),
    ("China", "Beijing"),
    ("Japan", "Tokyo"),
    ("Russia", "Moscow"),
    ("Egypt", "Cairo"),
]
# Asserted on the model's text, not the judge's transcription of the audio:
# Moonshine heard Deepgram's "Madrid" as "Majorid", failing a correct answer.
# One sentence a turn: a trailing "Just the city, please." let Smart Turn end
# the turn after the question, so the second half talked over the reply.
_SCENARIO = {
    "turns": [
        {
            "user": f"What is the capital of {country}?",
            "expect": [
                {"event": "llm_response", "text_contains": city, "within_ms": 30000}
            ],
        }
        for country, city in _CAPITALS
    ]
}
# A production agent's prompt is a few thousand characters, not one line; the
# delta-sending only matters if there is something not to resend.
# Past 1,024 tokens, too, or OpenAI's prompt cache never engages for either arm.
_POLICY = (
    "Callers may ask about opening hours (9am to 6pm weekdays, 10am to 2pm "
    "Saturdays, closed Sundays), returns (30 days with a receipt, store credit "
    "after that), delivery (two to four working days, free over fifty dollars) "
    "and loyalty points (one per dollar, redeemable in hundreds). "
)
_SYSTEM_PROMPT = (
    "You are a terse geography assistant on a phone line. Answer with the city "
    "name only. The line is noisy: if a country name sounds garbled, answer for "
    "the closest real country rather than saying you do not know. " + _POLICY * 20
)


def _blob() -> dict[str, Any]:
    return {
        "system_prompt": _SYSTEM_PROMPT,
        "first_message": None,
        "llm": {"provider": "openai", "model": _MODEL},
        "stt": {
            "provider": "deepgram",
            "model": "nova-3-general",
            "keyterms": [country for country, _ in _CAPITALS],
        },
        "tts": {"provider": "deepgram"},
        "smart_turn_detection": True,
    }


def _responses_stage(config: AgentConfig, credentials: Any, **kwargs: Any) -> Any:
    """The arm under test: the same model and sampling on the Responses API.

    No effort set leaves pipecat sending the model's lowest (`none` on 5.4).
    """
    effort = config.llm.reasoning_effort
    gpt5 = config.llm.model.startswith("gpt-5")
    return OpenAIResponsesLLMService(
        api_key=credentials.api_key("openai", config.llm.api_key),
        settings=OpenAIResponsesLLMService.Settings(
            model=config.llm.model,
            system_instruction=kwargs.get("system_instruction") or None,
            # gpt-5 models take only their default temperature.
            temperature=None if gpt5 else config.llm.temperature,
            max_completion_tokens=config.llm.max_tokens,
            reasoning=(
                OpenAIResponsesLLMService.ReasoningConfig(effort=effort)
                if effort
                else None
            ),
        ),
    )


class _LLMObserver(BaseObserver):
    """Each LLM response's time to first text, and every token usage report.

    Only frames the LLM itself pushes are read, so each is seen once.
    """

    def __init__(self, sink: dict[str, list]) -> None:
        super().__init__()
        self._sink = sink
        self._started_ns: int | None = None
        self._seen_metrics: set[int] = set()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if isinstance(frame, MetricsFrame) and frame.id not in self._seen_metrics:
            self._seen_metrics.add(frame.id)
            self._sink["usage"].extend(
                {
                    "input": m.value.prompt_tokens,
                    "cached": m.value.cache_read_input_tokens or 0,
                    "output": m.value.completion_tokens,
                    "reasoning": m.value.reasoning_tokens or 0,
                }
                for m in frame.data
                if isinstance(m, LLMUsageMetricsData)
            )
        if not isinstance(data.source, LLMService):
            return
        if isinstance(frame, LLMFullResponseStartFrame):
            self._started_ns = data.timestamp
        elif isinstance(frame, LLMTextFrame) and frame.text.strip():
            if self._started_ns is not None:
                ms = round((data.timestamp - self._started_ns) / 1e6)
                self._sink["first_text_ms"].append(ms)
                self._started_ns = None
        elif isinstance(frame, LLMFullResponseEndFrame) and self._started_ns:
            self._sink["cancelled"].append(1)
            self._started_ns = None


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Filled per conversation; the test empties it between them."""
    out: dict[str, list] = {
        "first_text_ms": [],
        "usage": [],
        "cancelled": [],
        "user_to_bot_ms": [],
    }

    async def write(payload: dict[str, Any]) -> None:
        out["user_to_bot_ms"].append(payload["user_to_bot_ms"])

    monkeypatch.setattr(
        turn_metrics,
        "turn_metrics_observers",
        lambda ctx: [
            turn_metrics.TurnMetricsRecorder(ctx, write=write).observer,
            _LLMObserver(out),
        ],
    )
    return out


async def _converse(
    session_factory: Any,
    blob: dict[str, Any] | None = None,
    scenario: dict[str, Any] = _SCENARIO,
    modality: EvalModality = EvalModality.AUDIO,
    tool_mocks: ToolMocks | None = None,
) -> Any:
    blob = blob or _blob()
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(scenario, modality),
        name="live-responses-latency",
    )
    return await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=ResolvedTarget(
            project_id=uuid4(),
            config=AgentConfig.model_validate(blob),
            config_blob=blob,
            agent_id=None,
        ),
        modality=modality,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=tool_mocks,
    )


def _p95(values: list[int]) -> int:
    """Nearest rank, as `latency_summary` computes it."""
    ordered = sorted(values)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


def _summarise(conversations: list[dict[str, list]]) -> dict[str, Any]:
    # The n-th answered response is the n-th turn: one answer a turn, and a
    # cancelled response carries no first text.
    by_turn: dict[int, list[int]] = {}
    for conv in conversations:
        for i, ms in enumerate(conv["first_text_ms"], start=1):
            by_turn.setdefault(i, []).append(ms)
    judged = [v for t, vs in by_turn.items() if t >= _JUDGED_FROM_TURN for v in vs]
    usage = [u for conv in conversations for u in conv["usage"]]
    n = len(conversations)
    return {
        "conversations": n,
        "median_by_turn": {
            t: statistics.median(vs) for t, vs in sorted(by_turn.items())
        },
        "judged_n": len(judged),
        "judged_median_ms": statistics.median(judged),
        "judged_p95_ms": _p95(judged),
        "user_to_bot_median_ms": statistics.median(
            v for conv in conversations for v in conv["user_to_bot_ms"]
        ),
        "cancelled_responses": sum(len(conv["cancelled"]) for conv in conversations),
        "input_tokens_per_conversation": sum(u["input"] for u in usage) / n,
        "cached_tokens_per_conversation": sum(u["cached"] for u in usage) / n,
        "output_tokens_per_conversation": sum(u["output"] for u in usage) / n,
    }


async def test_responses_websocket_against_chat_completions(
    openai_key: str,
    deepgram_key: str,
    session_factory: Any,
    sink: dict[str, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds = int(os.environ.get("RESPONSES_LATENCY_ROUNDS", "0"))
    if rounds < 1:
        pytest.skip("set RESPONSES_LATENCY_ROUNDS to run the #411 measurement")

    chat_stage = pipeline_factory._create_llm_stage
    arms: dict[str, list[dict[str, list]]] = {"chat": [], "responses": []}
    for _ in range(rounds):
        for arm, stage in (("chat", chat_stage), ("responses", _responses_stage)):
            monkeypatch.setattr(pipeline_factory, "_create_llm_stage", stage)
            result = await _converse(session_factory)
            assert result.passed, (arm, [str(f) for f in result.failures])
            arms[arm].append({k: list(v) for k, v in sink.items()})
            for values in sink.values():
                values.clear()

    chat, responses = _summarise(arms["chat"]), _summarise(arms["responses"])
    gain = 1 - responses["judged_median_ms"] / chat["judged_median_ms"]
    p95_ok = responses["judged_p95_ms"] <= chat["judged_p95_ms"]
    report = {
        "model": _MODEL,
        "chat": chat,
        "responses": responses,
        "verdict": {
            "median_gain": round(gain, 3),
            "p95_no_worse": p95_ok,
            "adopt": gain >= 0.15 and p95_ok,
        },
    }
    logger.info("responses_vs_chat {report}", report=json.dumps(report, indent=2))
    if out := os.environ.get("RESPONSES_LATENCY_OUT"):
        with open(out, "w") as fh:
            json.dump({**report, "raw": arms}, fh, indent=2)


_REASONING_MODEL = "gpt-5.4-mini"
_POSTCARD_TOOL = {
    "name": "send_postcard",
    "description": "Send the caller a postcard from a city.",
    "parameters_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
    "webhook_url": "https://postcards.invalid/send",
}
# Four answers timed with the tool on offer, then one turn that has to call it.
# Text modality: this times the LLM, and typed turns cannot be misheard.
_REASONING_SCENARIO = {
    "turns": [
        *_SCENARIO["turns"][:4],
        {
            "user": "Please send me a postcard from Tokyo.",
            "expect": [
                {
                    "event": "function_call",
                    "calls": [{"name": "send_postcard"}],
                    "within_ms": 30000,
                }
            ],
        },
    ]
}


async def test_a_reasoning_model_keeps_tools_and_reasoning_on_responses(
    openai_key: str,
    session_factory: Any,
    sink: dict[str, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds = int(os.environ.get("RESPONSES_REASONING_ROUNDS", "0"))
    if rounds < 1:
        pytest.skip("set RESPONSES_REASONING_ROUNDS to run the reasoning check")

    monkeypatch.setattr(pipeline_factory, "_create_llm_stage", _responses_stage)
    arms: dict[str, list[dict[str, list]]] = {"none": [], "low": []}
    for _ in range(rounds):
        for effort in arms:
            blob = {
                **_blob(),
                "llm": {
                    "provider": "openai",
                    "model": _REASONING_MODEL,
                    **({"reasoning_effort": effort} if effort != "none" else {}),
                },
                # "City name only" alone had one low-effort run answer "Tokyo"
                # instead of sending the postcard.
                "system_prompt": _SYSTEM_PROMPT
                + "When the caller asks for a postcard, call send_postcard.",
                "tools": [_POSTCARD_TOOL],
                # No audio in a text run for Smart Turn to read.
                "smart_turn_detection": False,
            }
            mocks = ToolMocks(responses={"send_postcard": {"sent": True}})
            result = await _converse(
                session_factory, blob, _REASONING_SCENARIO, EvalModality.TEXT, mocks
            )
            assert result.passed, (effort, [str(f) for f in result.failures])
            assert [c["tool_name"] for c in mocks.calls] == ["send_postcard"], (
                mocks.calls
            )
            arms[effort].append({k: list(v) for k, v in sink.items()})
            for values in sink.values():
                values.clear()

    report = {"model": _REASONING_MODEL}
    for effort, convs in arms.items():
        first = [ms for c in convs for ms in c["first_text_ms"]]
        usage = [u for c in convs for u in c["usage"]]
        report[effort] = {
            "answers": len(first),
            "median_first_text_ms": statistics.median(first),
            "p95_first_text_ms": _p95(first),
            "reasoning_tokens_per_conversation": sum(u["reasoning"] for u in usage)
            / len(convs),
        }
    logger.info("responses_reasoning {report}", report=json.dumps(report, indent=2))
    if out := os.environ.get("RESPONSES_LATENCY_OUT"):
        with open(out, "w") as fh:
            json.dump({**report, "raw": arms}, fh, indent=2)
