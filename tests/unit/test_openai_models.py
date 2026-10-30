"""What each OpenAI model family is sent (services/openai_models.py). The
expectations are OpenAI's answers, measured on 2026-10-30."""

import pytest

from turncall.services.openai_models import chat_params

pytestmark = pytest.mark.unit


def params(model: str, effort: str | None = None, tools: bool = False):
    return chat_params(
        model, temperature=0.7, max_tokens=1024, reasoning_effort=effort, tools=tools
    )


@pytest.mark.parametrize(
    "model", ["gpt-4o-mini", "gpt-4.1-mini", "gpt-5-mini", "gpt-5.4", "o3"]
)
def test_the_token_limit_is_always_max_completion_tokens(model):
    assert params(model).max_completion_tokens == 1024


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4.1-mini"])
def test_a_model_that_does_not_reason_gets_no_effort_and_its_temperature(model):
    p = params(model, "low", tools=True)
    assert (p.reasoning_effort, p.temperature) == (None, 0.7)
    assert "does not reason" in p.dropped[0]


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "gpt-5-nano", "o3"])
def test_a_model_that_always_reasons_gets_no_temperature(model):
    p = params(model)
    assert p.temperature is None and p.dropped == ()
    assert params(model, "low", tools=True).reasoning_effort == "low"


def test_minimal_is_gpt_5s_alone():
    assert params("gpt-5-mini", "minimal").reasoning_effort == "minimal"
    for model in ("gpt-5.1", "gpt-5.4-mini", "o3"):
        p = params(model, "minimal")
        assert p.reasoning_effort is None and "minimal" in p.dropped[0]


@pytest.mark.parametrize("model", ["gpt-5.1", "gpt-5.2", "gpt-5.4-mini"])
def test_a_later_gpt_5_takes_a_temperature_only_without_an_effort(model):
    assert params(model).temperature == 0.7
    p = params(model, "low")
    assert (p.reasoning_effort, p.temperature) == ("low", None)


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.4-nano-2026-03-05", "gpt-6"])
def test_gpt_5_4_and_later_take_no_effort_beside_tools(model):
    p = params(model, "low", tools=True)
    assert (p.reasoning_effort, p.temperature) == (None, 0.7)
    assert "function tools" in p.dropped[0]
    assert params("gpt-5.2", "low", tools=True).reasoning_effort == "low"
