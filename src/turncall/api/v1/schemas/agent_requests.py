"""The agent endpoints' request bodies."""

from typing import Annotated

from pydantic import BaseModel, Field, WrapValidator

from turncall.api.v1.schemas.agents import AgentConfigSchema

# A request carries the config raw: agent acceptance runs the schema on it
# (#478, `services/agent_acceptance`, the `api` origin), and the endpoints
# answer its errors exactly as body parsing did. The wrap validator never
# calls the schema; it wraps it so the OpenAPI document still `$ref`s it.
RawAgentConfig = Annotated[AgentConfigSchema, WrapValidator(lambda raw, _validate: raw)]


class CreateAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    environment: str = Field(
        default="development", pattern=r"^(development|staging|production)$"
    )
    config: RawAgentConfig = Field(default_factory=dict)


class UpdateAgentRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: RawAgentConfig | None = None
