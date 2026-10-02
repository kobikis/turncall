"""LLM failover end to end over the eval loopback (#222).

Ollama on both sides, so it runs on any box with one model pulled: the primary
names a model Ollama has never heard of — a 404, which pipecat marks unusable
— and the backup is a real one. No hosted key needed. The turn must still be answered, by the
backup, and the iteration must say a failover happened.
"""

import os
from typing import Any
from uuid import uuid4

import httpx
import pytest

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MISSING = "turncall-no-such-model"


@pytest.fixture
def ollama_model() -> str:
    try:
        tags = httpx.get(f"{OLLAMA}/api/tags", timeout=2.0)
        tags.raise_for_status()
        installed = [m["name"] for m in tags.json().get("models", [])]
    except Exception:
        pytest.skip(f"no Ollama at {OLLAMA}")
    if not installed:
        pytest.skip(f"Ollama at {OLLAMA} has no models pulled")
    return os.environ.get("EVAL_OLLAMA_MODEL") or installed[0]


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_an_unusable_llm_fails_over_and_the_turn_is_answered(
    ollama_model: str, session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only so the unused OpenAI speech services construct; nothing calls them.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-unused")
    base_url = f"{OLLAMA}/v1"
    blob = {
        "system_prompt": "You are a terse geography assistant.",
        "llm": {
            "provider": "ollama",
            "model": MISSING,
            "base_url": base_url,
            "failover": [
                {"provider": "ollama", "model": ollama_model, "base_url": base_url}
            ],
        },
        "smart_turn_detection": False,
        # Text mode never uses them, but Deepgram's open a socket at start and
        # a box without its key would kill the pipeline before the first turn.
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
                "user": "What is the capital of France? Answer with just the city.",
                "expect": [
                    {
                        "event": "llm_response",
                        "text_contains": "Paris",
                        "within_ms": 120000,
                    }
                ],
            }
        ]
    }
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.TEXT), name="live-failover"
    )
    agent_errors: list[str] = []
    result = await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.TEXT,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=None,
        agent_errors=agent_errors,
    )

    assert result.passed, ([str(f) for f in result.failures], agent_errors)
    assert any(
        e.startswith(f"llm failed over from ollama/{MISSING} to ollama/{ollama_model}")
        for e in agent_errors
    ), agent_errors
