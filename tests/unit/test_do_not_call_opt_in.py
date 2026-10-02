"""`do_not_call` is opt-in (#228): an agent has it only by listing it."""

import pytest

from turncall.api.v1.schemas.agents import ToolDefinitionSchema
from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.orchestrator.pipeline_factory import _build_tools_schema

pytestmark = pytest.mark.unit

DNC = ToolDefinition(
    name="do_not_call",
    description="The caller asked not to be called again. Say goodbye first.",
    parameters_schema={"type": "object", "properties": {}},
)
END = DNC.model_copy(update={"name": "end_call", "description": "Hang up."})


def _advertised(config: AgentConfig) -> list[str]:
    schema = _build_tools_schema(config)
    return [] if schema is None else [f.name for f in schema.standard_tools]


def test_absent_unless_the_agent_lists_it():
    assert "do_not_call" not in _advertised(AgentConfig(tools=[END]))
    assert "do_not_call" in _advertised(AgentConfig(tools=[END, DNC]))


def test_enabled_as_a_builtin_without_a_webhook():
    ToolDefinitionSchema(name="do_not_call", description="Opt the caller out.")
