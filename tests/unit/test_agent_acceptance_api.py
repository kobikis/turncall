"""Agent acceptance's `api` origin (#478): agent create and update.

The API's whole schema runs inside acceptance, so its refusals carry the
schema's own errors — what the agent endpoints answer as a 422
`validation_error`, unchanged — and what it accepts is stored as the
schema writes it. Against the `call_init` origin, the same input: an unknown
field and a plain http URL are refused here, warned about and allowed there.
"""

import uuid
from typing import Any

import pytest

from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.config.settings import Environment, get_settings
from turncall.domain.models import AgentConfig
from turncall.services.agent_acceptance import (
    POLICIES,
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    accept_agent,
)

pytestmark = pytest.mark.unit

PROJECT = uuid.uuid4()
API = AgentOrigin.API
CALL_INIT = AgentOrigin.CALL_INIT

PLAIN_HTTP_TOOL = {
    "tools": [
        {
            "name": "lookup",
            "description": "Look the caller up",
            "parameters_schema": {"type": "object", "properties": {}},
            "webhook_url": "http://crm.example/lookup",
        }
    ]
}


async def _accept(source: Any, origin: AgentOrigin) -> AcceptedAgent | AgentRefusal:
    return await accept_agent(source, project_id=PROJECT, origin=origin, session=None)


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(settings, "allow_plain_http_urls", None)


# --- what the api origin accepts -------------------------------------------------


async def test_a_valid_config_is_accepted_as_the_schema_stores_it() -> None:
    raw = {"system_prompt": "Hello", "stt": {"keyterms": ["Acme", "Acme", " "]}}

    accepted = await _accept(raw, API)

    assert isinstance(accepted, AcceptedAgent)
    assert accepted.agent_id is None
    stored = AgentConfigSchema.model_validate(raw).model_dump()
    assert accepted.stored == stored
    assert accepted.config == AgentConfig.model_validate(stored)


async def test_an_empty_config_is_the_default_agent() -> None:
    accepted = await _accept({}, API)

    assert isinstance(accepted, AcceptedAgent)
    assert accepted.stored == AgentConfigSchema().model_dump()


# --- unknown fields: refused on api, warned about on call_init --------------------


async def test_the_api_origin_refuses_an_unknown_field_with_the_schemas_error() -> None:
    refusal = await _accept({"system_prompt": "hi", "voice_style": "warm"}, API)

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field == "voice_style"
    [error] = refusal.schema_errors
    assert error["type"] == "extra_forbidden"
    assert error["loc"] == ("voice_style",)


async def test_the_call_init_origin_warns_about_the_same_unknown_field() -> None:
    accepted = await _accept({"system_prompt": "hi", "voice_style": "warm"}, CALL_INIT)

    assert isinstance(accepted, AcceptedAgent)
    assert [w.code for w in accepted.warnings] == ["unknown_field"]


# --- plain http (#340): refused on api outside development, allowed on call_init -


async def test_the_api_origin_refuses_a_plain_http_url_outside_development(
    production: None,
) -> None:
    refusal = await _accept(PLAIN_HTTP_TOOL, API)

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field == "tools[0].webhook_url"
    assert "https" in refusal.reason
    assert refusal.schema_errors[0]["loc"] == ("tools", 0, "webhook_url")


async def test_the_call_init_origin_allows_the_same_plain_http_url(
    production: None,
) -> None:
    accepted = await _accept(PLAIN_HTTP_TOOL, CALL_INIT)

    assert isinstance(accepted, AcceptedAgent)
    assert accepted.config.tools[0].webhook_url == "http://crm.example/lookup"


async def test_the_api_origin_allows_a_plain_http_url_in_development() -> None:
    settings = get_settings()
    assert settings.environment == Environment.DEVELOPMENT

    assert isinstance(await _accept(PLAIN_HTTP_TOOL, API), AcceptedAgent)


# --- every schema refusal comes back whole ---------------------------------------


async def test_a_schema_validator_refusal_keeps_its_words() -> None:
    refusal = await _accept(
        {"llm": {"provider": "xai", "base_url": "https://gw.example/v1"}}, API
    )

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field == "llm"
    assert "base_url is not supported for the xai provider" in refusal.reason
    assert refusal.schema_errors[0]["type"] == "value_error"


async def test_every_schema_error_is_kept_not_only_the_first() -> None:
    refusal = await _accept({"zeta": 1, "alpha": 2}, API)

    assert isinstance(refusal, AgentRefusal)
    assert [e["loc"] for e in refusal.schema_errors] == [("zeta",), ("alpha",)]


async def test_a_config_that_is_not_an_object_is_refused_by_the_schema() -> None:
    refusal = await _accept(["system_prompt"], API)  # type: ignore[arg-type]

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field is None
    assert refusal.schema_errors[0]["loc"] == ()


# --- a stored agent's knowledge bases are its links (#260) ------------------------


async def test_the_api_origin_refuses_inline_knowledge_bases() -> None:
    refusal = await _accept(
        {"knowledge_bases": [{"knowledge_base_id": str(uuid.uuid4())}]}, API
    )

    assert isinstance(refusal, AgentRefusal)
    assert refusal.field == "knowledge_bases"
    assert refusal.reason.startswith("knowledge_bases is for an inline agent only")
    assert refusal.schema_errors == ()


def test_only_the_api_origin_refuses_inline_knowledge_bases() -> None:
    refusing = {o for o, p in POLICIES.items() if not p.inline_knowledge_bases}

    assert refusing == {API}


async def test_an_empty_knowledge_bases_list_is_not_refused() -> None:
    assert isinstance(await _accept({"knowledge_bases": []}, API), AcceptedAgent)


async def test_the_api_origin_takes_no_agent_id() -> None:
    with pytest.raises(TypeError):
        await _accept(uuid.uuid4(), API)
