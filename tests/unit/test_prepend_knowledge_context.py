"""Tests for prepend_knowledge_context in template_renderer."""

import pytest

from turncall.domain.models import AgentConfig
from turncall.services.template_renderer import prepend_knowledge_context


@pytest.mark.unit
class TestPrependKnowledgeContext:
    def test_prepends_to_system_prompt(self) -> None:
        config = AgentConfig(system_prompt="You are a helpful assistant.")
        result = prepend_knowledge_context(config, "Customer has open ticket #123.")

        assert result.system_prompt.startswith("Customer has open ticket #123.")
        assert "You are a helpful assistant." in result.system_prompt

    def test_separates_with_double_newline(self) -> None:
        config = AgentConfig(system_prompt="Base prompt.")
        result = prepend_knowledge_context(config, "Context info.")

        assert result.system_prompt == "Context info.\n\nBase prompt."

    def test_empty_string_returns_same_config(self) -> None:
        config = AgentConfig(system_prompt="Original.")
        result = prepend_knowledge_context(config, "")

        assert result.system_prompt == "Original."

    def test_whitespace_only_returns_same_config(self) -> None:
        config = AgentConfig(system_prompt="Original.")
        result = prepend_knowledge_context(config, "   \n  ")

        assert result.system_prompt == "Original."

    def test_strips_leading_trailing_whitespace(self) -> None:
        config = AgentConfig(system_prompt="Base.")
        result = prepend_knowledge_context(config, "  Context with spaces  ")

        assert result.system_prompt == "Context with spaces\n\nBase."

    def test_original_config_unchanged(self) -> None:
        config = AgentConfig(system_prompt="Original.")
        prepend_knowledge_context(config, "New context.")

        assert config.system_prompt == "Original."

    def test_does_not_affect_other_fields(self) -> None:
        config = AgentConfig(
            system_prompt="Base.",
            first_message="Hello!",
        )
        result = prepend_knowledge_context(config, "Context.")

        assert result.first_message == "Hello!"
