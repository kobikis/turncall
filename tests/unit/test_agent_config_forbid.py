"""AgentConfigSchema rejects unknown fields (review: silent-drop of typo'd
config) and stays in sync with the domain model's settable fields."""

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig


@pytest.mark.unit
def test_rejects_unknown_field():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentConfigSchema(systemPrompt="typo camelCase")  # should be system_prompt


@pytest.mark.unit
def test_rejects_misnested_section():
    # A mis-nested section (the avatar-config class of bug) now fails loudly.
    with pytest.raises(ValidationError):
        AgentConfigSchema(avatr={"enabled": True})


@pytest.mark.unit
def test_valid_config_still_accepted():
    cfg = AgentConfigSchema(
        system_prompt="hi",
        llm={"provider": "openai", "model": "gpt-4o-mini"},
        tools=[{"name": "end_call", "description": "end"}],
    )
    assert cfg.system_prompt == "hi"


@pytest.mark.unit
def test_schema_covers_domain_settable_fields():
    """Every domain AgentConfig field must be declared on the schema (else
    extra='forbid' would reject a valid round-tripped config). knowledge_bases
    is deliberately excluded — it's attached via a separate endpoint."""
    schema_fields = set(AgentConfigSchema.model_fields)
    domain_fields = set(AgentConfig.model_fields)
    missing = domain_fields - schema_fields - {"knowledge_bases"}
    assert not missing, f"schema is missing domain fields: {missing}"
