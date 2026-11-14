"""Agent acceptance for eval targets and the demo agents file (#479).

The `eval_target` row is a stored agent's config — an `agent` or `agent_name`
target — checked by the rules the build holds, so a config that cannot run
errors the run before an iteration. The `eval_inline` and `demo` rows run the
API's whole agent schema, as each did before acceptance, with plain http
allowed (#340): what they refuse is unchanged.
"""

import uuid
from typing import Any

import pytest

from turncall.services.agent_acceptance import (
    POLICIES,
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    UnknownFields,
    accept_agent,
    accept_raw_config,
)

pytestmark = pytest.mark.unit

PROJECT = uuid.uuid4()
EVAL_TARGET = AgentOrigin.EVAL_TARGET
SCHEMA_ORIGINS = (AgentOrigin.EVAL_INLINE, AgentOrigin.DEMO)
XAI_GATEWAY = {"llm": {"provider": "xai", "base_url": "https://gw.example/v1"}}
IPA = "mɛtˈfɔɹmɪn"  # noqa: RUF001


async def _accept(config: Any, origin: AgentOrigin) -> AcceptedAgent | AgentRefusal:
    return await accept_agent(config, project_id=PROJECT, origin=origin, session=None)


async def _refused(config: Any, origin: AgentOrigin) -> AgentRefusal:
    outcome = await _accept(config, origin)
    assert isinstance(outcome, AgentRefusal), outcome
    return outcome


async def _accepted(config: Any, origin: AgentOrigin) -> AcceptedAgent:
    outcome = await _accept(config, origin)
    assert isinstance(outcome, AcceptedAgent), outcome
    return outcome


def test_the_three_rows_are_wired_with_the_full_schema_only_where_it_ran() -> None:
    for origin in (EVAL_TARGET, *SCHEMA_ORIGINS):
        assert POLICIES[origin].wired_by is None
    assert POLICIES[EVAL_TARGET].full_schema is False
    assert POLICIES[EVAL_TARGET].unknown_fields is UnknownFields.WARN
    for origin in SCHEMA_ORIGINS:
        assert POLICIES[origin].full_schema is True


# --- eval_target ------------------------------------------------------------------


async def test_an_eval_target_breaking_a_provider_rule_is_refused_naming_it() -> None:
    refusal = await _refused(XAI_GATEWAY, EVAL_TARGET)

    assert refusal.field == "llm"
    assert refusal.describe() == "llm: base_url is not supported for the xai provider"


async def test_an_eval_target_with_a_removed_field_is_warned_not_refused() -> None:
    """A stored config was written through the schema: a key it no longer
    knows is one a later release removed."""
    accepted = await _accepted({"system_prompt": "hi", "retired": 1}, EVAL_TARGET)

    assert [w.field for w in accepted.warnings] == ["retired"]


async def test_an_eval_target_is_not_held_to_the_schemas_allowlists() -> None:
    """Only the rules the build holds: a model the schema would require is
    not, since the agent was saved before that rule existed and still builds."""
    accepted = await _accepted({"llm": {"provider": "ollama"}}, EVAL_TARGET)

    assert accepted.config.llm.provider == "ollama"


# --- eval_inline and demo ---------------------------------------------------------


@pytest.mark.parametrize("origin", SCHEMA_ORIGINS)
async def test_an_unknown_field_is_refused(origin: AgentOrigin) -> None:
    refusal = await _refused({"sytem_prompt": "typo"}, origin)

    assert refusal.field == "sytem_prompt"
    assert "sytem_prompt" in refusal.describe()


@pytest.mark.parametrize("origin", SCHEMA_ORIGINS)
async def test_a_schema_only_rule_is_refused_naming_the_field(
    origin: AgentOrigin,
) -> None:
    """`model_is_required`: the API's schema refuses an Ollama LLM with no
    model, which the build alone never checked."""
    refusal = await _refused({"llm": {"provider": "ollama"}}, origin)

    assert refusal.field == "llm"
    assert refusal.describe().startswith("llm: ")
    assert "Value error" not in refusal.reason


@pytest.mark.parametrize("origin", SCHEMA_ORIGINS)
async def test_a_strict_nested_section_is_refused_by_its_path(
    origin: AgentOrigin,
) -> None:
    refusal = await _refused(
        {"skills": [{"skill": "explain_bill", "pinned": True}]}, origin
    )

    assert refusal.field == "skills[0].pinned"


@pytest.mark.parametrize("origin", SCHEMA_ORIGINS)
async def test_a_provider_rule_is_refused(origin: AgentOrigin) -> None:
    refusal = await _refused({"llm": {**XAI_GATEWAY["llm"], "model": "grok-4"}}, origin)

    assert refusal.describe() == "llm: base_url is not supported for the xai provider"


@pytest.mark.parametrize("origin", SCHEMA_ORIGINS)
async def test_plain_http_is_allowed(origin: AgentOrigin) -> None:
    tool = {
        "name": "book",
        "description": "Book it",
        "parameters_schema": {},
        "webhook_url": "http://tools.example/book",
    }
    accepted = await _accepted({"tools": [tool]}, origin)

    assert accepted.config.tools[0].webhook_url == "http://tools.example/book"


@pytest.mark.parametrize("origin", SCHEMA_ORIGINS)
async def test_the_config_is_the_one_a_save_would_store_inline_kbs_kept(
    origin: AgentOrigin,
) -> None:
    """The schema's normal form (pronunciations canonical), and the inline
    knowledge bases its dump leaves out (`exclude=True`) kept: the demo used
    to lose them, so its knowledge-base refusal could never fire."""
    kb = {"knowledge_base_id": str(uuid.uuid4()), "mode": "prompt"}
    tts = {"provider": "cartesia", "pronunciations": {"Metformin": f"/{IPA}/"}}
    accepted = await _accepted({"knowledge_bases": [kb], "tts": tts}, origin)

    assert [str(k.knowledge_base_id) for k in accepted.config.knowledge_bases] == [
        kb["knowledge_base_id"]
    ]
    assert accepted.config.tts.pronunciations == {"Metformin": IPA}


@pytest.mark.parametrize("origin", SCHEMA_ORIGINS)
async def test_a_config_that_is_not_an_object_is_refused(origin: AgentOrigin) -> None:
    refusal = await _refused(["not", "an", "object"], origin)

    assert refusal.field is None


def test_a_raw_config_is_accepted_with_no_event_loop() -> None:
    """The demo loads its file before any loop runs, and inside one in tests."""
    accepted = accept_raw_config({"system_prompt": "hi"}, origin=AgentOrigin.DEMO)

    assert isinstance(accepted, AcceptedAgent)
    assert accepted.agent_id is None


def test_describe_names_the_field_once() -> None:
    assert AgentRefusal("bad", "llm").describe() == "llm: bad"
    assert AgentRefusal("llm.model: bad", "llm.model").describe() == "llm.model: bad"
    assert AgentRefusal("bad").describe() == "bad"
