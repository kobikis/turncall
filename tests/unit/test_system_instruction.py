"""The system prompt travels as `system_instruction`, not as a context message.

Pipecat deprecated setting the system prompt through `LLMContext` in 1.9 and
drops it in 2.0. It is also not a thing you can do twice: the OpenAI adapter
prepends `system_instruction` to the context messages, so a prompt in both
places is sent twice — which is what made the handoff site move at the same
time as the two build-time ones.
"""

import pytest

from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator.pipeline_factory import (
    _build_system_instruction,
    _create_llm_service,
)


@pytest.mark.unit
class TestBuildSystemInstruction:
    def test_plain_prompt(self) -> None:
        cfg = AgentConfig(system_prompt="You are Ada.")
        assert _build_system_instruction(cfg) == "You are Ada."

    def test_guardrails_are_appended(self) -> None:
        cfg = AgentConfig(
            system_prompt="You are Ada.",
            guardrails={"prohibited_topics": ["politics"]},
        )
        out = _build_system_instruction(cfg)
        assert out.startswith("You are Ada.")
        assert "politics" in out

    def test_knowledge_preamble_leads(self) -> None:
        """Prompt-mode documents sit in front of the agent's own words."""
        cfg = AgentConfig(system_prompt="You are Ada.")
        out = _build_system_instruction(cfg, knowledge_preamble="MENU: sushi, ramen")
        assert out == "MENU: sushi, ramen\n\nYou are Ada."

    def test_preamble_alone_when_no_prompt(self) -> None:
        cfg = AgentConfig(system_prompt="")
        assert _build_system_instruction(cfg, knowledge_preamble="FACTS") == "FACTS"

    def test_tools_prompt_only_when_asked(self) -> None:
        cfg = AgentConfig(
            system_prompt="You are Ada.",
            tools=[
                {
                    "name": "check_stock",
                    "description": "Check stock",
                    "parameters_schema": {"type": "object", "properties": {}},
                }
            ],  # type: ignore[list-item]
        )
        assert "check_stock" not in _build_system_instruction(cfg)
        assert "check_stock" in _build_system_instruction(cfg, inject_tools_prompt=True)


@pytest.mark.unit
class TestItReachesTheService:
    def test_openai_receives_it(self) -> None:
        cfg = AgentConfig(system_prompt="hi", llm=LLMConfig(provider="openai"))
        svc = _create_llm_service(cfg, "sk-test", system_instruction="You are Ada.")
        assert svc._settings.system_instruction == "You are Ada."

    def test_anthropic_receives_it(self) -> None:
        cfg = AgentConfig(
            system_prompt="hi",
            llm=LLMConfig(provider="anthropic", model="claude-3-5-haiku-20241022"),
        )
        svc = _create_llm_service(
            cfg, "", anthropic_api_key="sk-ant", system_instruction="Ada"
        )
        assert svc._settings.system_instruction == "Ada"

    def test_empty_leaves_the_provider_default(self) -> None:
        """Empty means unset, not an empty system prompt."""
        cfg = AgentConfig(system_prompt="hi", llm=LLMConfig(provider="openai"))
        svc = _create_llm_service(cfg, "sk-test")
        assert not svc._settings.system_instruction
