"""A voice conversation on a `google` LLM agent that calls a tool (#205).

Over the eval loopback (ADR-0018), so the agent's whole cascade pipeline is
built the way a call builds it — `build_call_pipeline`, the real
`GoogleLLMService`, TurnCall's tool bridge registering the handlers, pipecat's
own function-call round trip and context aggregation. Only Google's servers
are replaced: the service's genai client answers from a script, and records
what it was sent. The tool is mocked through the scenario's `tool_mocks`, as an
eval would.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.services.credentials import Credentials
from turncall.services.tool_mocks import ToolMocks

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _chunk(part: dict[str, Any]) -> Any:
    from google.genai.types import GenerateContentResponse

    return GenerateContentResponse.model_validate(
        {"candidates": [{"content": {"role": "model", "parts": [part]}}]}
    )


class _Gemini:
    """Stands in for google-genai's client, answering from what it was sent.

    The agent opens the call with a turn of its own before the caller says
    anything, so answers depend on the conversation rather than on order: a
    greeting, then a call to the tool once the caller asks, then the answer
    once the tool's result is in the contents.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.aio = SimpleNamespace(
            models=SimpleNamespace(generate_content_stream=self._stream),
            aclose=self._aclose,
        )

    def _answer(self, contents: list[Any]) -> Any:
        parts = [p for c in contents for p in (c.parts or [])]
        if any(p.function_response for p in parts):
            return _chunk({"text": "Order 42 shipped on Monday."})
        if any(p.text and "order 42" in p.text for p in parts):
            return _chunk(
                {
                    "function_call": {
                        "name": "lookup_order",
                        "args": {"order_id": "42"},
                    },
                    "thought_signature": b"signed",
                }
            )
        return _chunk({"text": "Hello, how can I help?"})

    async def _stream(self, **request: Any) -> Any:
        self.requests.append(request)
        answer = self._answer(request["contents"])

        async def chunks() -> Any:
            yield answer

        return chunks()

    async def _aclose(self) -> None:
        pass


class _NoDb:
    """A session factory for a target with no knowledge bases: entered, never
    queried."""

    def __call__(self) -> "_NoDb":
        return self

    async def __aenter__(self) -> MagicMock:
        return MagicMock()

    async def __aexit__(self, *_: object) -> bool:
        return False


async def test_a_google_agent_calls_its_tool_and_answers_from_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipecat.services.google.llm import GoogleLLMService

    gemini = _Gemini()
    monkeypatch.setattr(
        GoogleLLMService, "create_client", lambda self: setattr(self, "_client", gemini)
    )
    blob = {
        "system_prompt": "You look up orders.",
        "llm": {"provider": "google"},
        "tools": [
            {
                "name": "lookup_order",
                "description": "Look up an order by id",
                "parameters_schema": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
                "webhook_url": "https://orders.example/lookup",
            }
        ],
        "smart_turn_detection": False,
        # Never used in text mode; chosen so nothing opens a socket at start.
        "stt": {"provider": "openai"},
        "tts": {"provider": "openai", "voice": "alloy"},
    }
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    definition = {
        "turns": [
            {
                "user": "Where is order 42?",
                "expect": [
                    {
                        "event": "function_call",
                        "calls": [{"name": "lookup_order", "args": {"order_id": "42"}}],
                    },
                    {"event": "llm_response", "text_contains": "shipped"},
                ],
            }
        ]
    }
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.TEXT), name="gemini-tools"
    )
    mocks = ToolMocks(responses={"lookup_order": {"status": "shipped Monday"}})
    keys = Credentials(platform={"google": "g-key", "openai": "sk-unused"})
    agent_errors: list[str] = []

    with patch(
        "turncall.orchestrator.pipeline_builder.load_call_credentials",
        return_value=keys,
    ):
        result = await run_iteration(
            parsed=parsed,
            kind=EvalKind.SCRIPTED,
            target=target,
            modality=EvalModality.TEXT,
            settings=Settings(),
            session_factory=_NoDb(),
            run_id=uuid4(),
            tool_mocks=mocks,
            timeout_s=60,
            agent_errors=agent_errors,
        )

    assert result.passed, ([str(f) for f in result.failures], agent_errors)
    assert [c["tool_name"] for c in mocks.calls] == ["lookup_order"]
    declared = gemini.requests[0]["config"].tools[0].function_declarations
    assert [d.name for d in declared] == ["lookup_order"]
    # The tool's answer went back to Gemini on the follow-up, beside the
    # signed call it answers — the signature is what Gemini 3 checks.
    follow_up = next(
        r["contents"]
        for r in gemini.requests
        if any(p.function_response for p in r["contents"][-1].parts or [])
    )
    model_turn, tool_turn = follow_up[-2:]
    assert model_turn.parts[0].thought_signature == b"signed"
    response = tool_turn.parts[0].function_response
    assert response.name == "lookup_order"
    assert "shipped Monday" in str(response.response)
