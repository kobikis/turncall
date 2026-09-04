"""Tests for template variable rendering."""

import pytest

from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.services.template_renderer import render_agent_config, render_template


@pytest.mark.unit
class TestRenderTemplate:
    def test_basic_replacement(self) -> None:
        result = render_template("Hello {{name}}", {"name": "John"})
        assert result == "Hello John"

    def test_multiple_variables(self) -> None:
        result = render_template(
            "{{name}} has account {{account_id}}",
            {"name": "Jane", "account_id": "A-123"},
        )
        assert result == "Jane has account A-123"

    def test_unmatched_placeholder_kept(self) -> None:
        result = render_template("Hello {{name}}, {{unknown}}", {"name": "John"})
        assert result == "Hello John, {{unknown}}"

    def test_no_variables_no_change(self) -> None:
        result = render_template("Hello world", {})
        assert result == "Hello world"

    def test_no_placeholders_no_change(self) -> None:
        result = render_template("Hello world", {"name": "John"})
        assert result == "Hello world"

    def test_repeated_variable(self) -> None:
        result = render_template("{{x}} and {{x}}", {"x": "yes"})
        assert result == "yes and yes"

    def test_empty_string(self) -> None:
        result = render_template("", {"name": "John"})
        assert result == ""


@pytest.mark.unit
class TestRenderAgentConfig:
    def test_renders_system_prompt(self) -> None:
        config = AgentConfig(
            system_prompt="You are helping {{name}} with account {{account_id}}",
        )
        rendered = render_agent_config(
            config, {"name": "Alice", "account_id": "X-99"}
        )
        assert "Alice" in rendered.system_prompt
        assert "X-99" in rendered.system_prompt

    def test_renders_first_message(self) -> None:
        config = AgentConfig(
            first_message="Welcome back, {{name}}!",
        )
        rendered = render_agent_config(config, {"name": "Bob"})
        assert rendered.first_message == "Welcome back, Bob!"

    def test_renders_tool_descriptions(self) -> None:
        config = AgentConfig(
            tools=[
                ToolDefinition(
                    name="transfer_call",
                    description="Transfer {{name}} to their agent at {{agent_number}}",
                    parameters_schema={},
                    is_builtin=True,
                ),
            ],
        )
        rendered = render_agent_config(
            config, {"name": "Carol", "agent_number": "+1555123"}
        )
        assert "Carol" in rendered.tools[0].description
        assert "+1555123" in rendered.tools[0].description

    def test_empty_variables_returns_same_config(self) -> None:
        config = AgentConfig(system_prompt="Hello {{name}}")
        rendered = render_agent_config(config, {})
        assert rendered.system_prompt == "Hello {{name}}"

    def test_original_config_unchanged(self) -> None:
        config = AgentConfig(system_prompt="Hello {{name}}")
        render_agent_config(config, {"name": "Dave"})
        assert config.system_prompt == "Hello {{name}}"

    def test_none_first_message_stays_none(self) -> None:
        config = AgentConfig(first_message=None)
        rendered = render_agent_config(config, {"name": "Eve"})
        assert rendered.first_message is None
