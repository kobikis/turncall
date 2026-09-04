"""Guardrails enforcement: prohibited_topics inject into the system prompt."""

import pytest

from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_factory import (
    _build_guardrails_section,
    _build_system_messages,
)


@pytest.mark.unit
def test_prohibited_topics_injected_into_system_prompt():
    cfg = AgentConfig(
        system_prompt="You are a support agent.",
        guardrails={"prohibited_topics": ["politics", "medical advice"]},
    )
    content = _build_system_messages(cfg)[0]["content"]
    assert "You are a support agent." in content  # original prompt preserved
    assert "Guardrails" in content
    assert "politics" in content
    assert "medical advice" in content
    assert "politely decline" in content


@pytest.mark.unit
def test_no_guardrails_leaves_prompt_untouched():
    cfg = AgentConfig(system_prompt="Hi.")
    assert _build_system_messages(cfg)[0]["content"] == "Hi."


@pytest.mark.unit
def test_blank_topics_are_ignored():
    cfg = AgentConfig(system_prompt="Hi.", guardrails={"prohibited_topics": ["", "  "]})
    assert _build_guardrails_section(cfg) == ""
    assert _build_system_messages(cfg)[0]["content"] == "Hi."
