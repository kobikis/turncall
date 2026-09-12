"""C5 — Claude on Bedrock.

This is the one gap that is blocked rather than unwritten. The AWS account can
*list* Anthropic models and cannot invoke them:

    converse(anthropic.claude-sonnet-5)
      -> AccessDeniedException: not available for this account
    converse(us.anthropic.claude-sonnet-4-5-...)
      -> ResourceNotFoundException: Model use case details have not been
         submitted for this account

Until that form is submitted, the Bedrock temperature rule rests on an argument
rather than an observation: the deprecation belongs to the model, Bedrock is a
gateway, and sending a rejected temperature breaks every call while withholding
an accepted one only costs the provider's default.

The tests are written so that the day access is granted, they answer the
question rather than needing to be invented then.
"""

import os

import pytest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_MODELS = ["us.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-5"]


def _client():
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        pytest.skip("AWS_ACCESS_KEY_ID is not set")
    import boto3

    return boto3.client(
        "bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1")
    )


def _converse(client, model: str, *, temperature: float | None):
    config: dict = {"maxTokens": 8}
    if temperature is not None:
        config["temperature"] = temperature
    return client.converse(
        modelId=model,
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        inferenceConfig=config,
    )


def _skip_if_no_model_access(exc: Exception) -> None:
    text = str(exc)
    for blocked in ("not available for this account", "use case details"):
        if blocked in text:
            pytest.skip(f"Bedrock model access not granted: {text[:120]}")


@pytest.mark.parametrize("model", _MODELS)
async def test_claude_on_bedrock_answers_without_a_temperature(model: str) -> None:
    """What the code now sends. If this fails, the rule is wrong."""
    client = _client()
    try:
        response = _converse(client, model, temperature=None)
    except Exception as exc:
        _skip_if_no_model_access(exc)
        raise

    assert response["output"]["message"]["content"]


@pytest.mark.parametrize("model", _MODELS)
async def test_whether_bedrock_rejects_a_temperature_for_claude(model: str) -> None:
    """The question the code could not answer. Whichever way this goes, it is
    an observation instead of an argument — and if Bedrock turns out to accept
    one, the predicate in services/bedrock_models.py can be narrowed to the
    Claude 5 family with evidence behind it."""
    client = _client()
    try:
        _converse(client, model, temperature=0.7)
    except Exception as exc:
        _skip_if_no_model_access(exc)
        assert "temperature" in str(exc).lower(), (
            f"{model} failed with a temperature for some other reason: {exc}"
        )
        return

    pytest.fail(
        f"{model} ACCEPTED a temperature on Bedrock — the rule in "
        "services/bedrock_models.py can be narrowed to the Claude 5 family"
    )
