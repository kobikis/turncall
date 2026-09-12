"""Claude on Bedrock gets no temperature, same as Claude direct.

Current Claude models answer `400 "temperature is deprecated for this model."`
— probed against the live API: claude-sonnet-5 and claude-opus-5 reject it,
claude-haiku-4-5 and claude-sonnet-4-5 still accept it. #38 stopped sending it
on the direct path, where an unwanted default ended the call on its first LLM
turn.

Bedrock is a *gateway* (ADR-0016), not a vendor: the deprecation belongs to the
model, so it should travel with the model rather than the endpoint. That could
not be confirmed against the live service — Bedrock's Claude models are behind
an account-level use-case approval here — so this is the asymmetry argument
rather than an observation. Sending a rejected temperature breaks every call;
withholding an accepted one costs the provider's default instead of 0.7. The
other vendors Bedrock fronts are untouched.
"""

from unittest.mock import patch

import pytest

from turncall.domain.models import AgentConfig, AWSConfig, LLMConfig
from turncall.services.bedrock_models import is_anthropic_model


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("anthropic.claude-sonnet-5", True),
        # Cross-region inference prefixes the vendor, it doesn't replace it.
        ("us.anthropic.claude-sonnet-5", True),
        ("eu.anthropic.claude-opus-4-5-20251101-v1:0", True),
        # An inference-profile ARN buries the id after a slash.
        (
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
            "us.anthropic.claude-sonnet-5",
            True,
        ),
        ("meta.llama3-3-70b-instruct-v1:0", False),
        ("mistral.mistral-large-2407-v1:0", False),
        ("amazon.nova-pro-v1:0", False),
        ("", False),
    ],
)
def test_vendor_is_read_off_the_model_id(model_id: str, expected: bool) -> None:
    assert is_anthropic_model(model_id) is expected


def _config(model: str, temperature: float = 0.25) -> AgentConfig:
    return AgentConfig(
        llm=LLMConfig(
            provider="bedrock", model=model, temperature=temperature, max_tokens=512
        ),
        aws=AWSConfig(access_key_id="AKIA", secret_access_key="s", region="us-east-1"),
    )


@pytest.mark.unit
class TestVoicePath:
    def test_claude_on_bedrock_is_sent_no_temperature(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        service = _create_llm_service(_config("us.anthropic.claude-sonnet-5"), "k")

        assert service._settings.temperature is None
        # The rest of the config still arrives — this withholds one field.
        assert service._settings.max_tokens == 512

    def test_another_vendor_on_bedrock_still_gets_one(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        service = _create_llm_service(_config("meta.llama3-3-70b-instruct-v1:0"), "k")

        assert service._settings.temperature == 0.25

    def test_the_voicemail_classifier_pin_is_dropped_too_on_claude(self) -> None:
        """The classifier pins temperature=0.1 for determinism. On a model that
        rejects the field, a rejected request is worse than a warmer one — same
        trade the direct Anthropic path already makes."""
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        service = _create_llm_service(
            _config("anthropic.claude-opus-5"), "k", temperature=0.1
        )

        assert service._settings.temperature is None


@pytest.mark.unit
@pytest.mark.asyncio
class TestTextPath:
    async def _converse_kwargs(self, model: str) -> dict:
        """Run _complete_text_bedrock against a stubbed client and return the
        kwargs it would have sent."""
        from turncall.services import llm_text

        captured: dict = {}

        class _Client:
            def converse(self, **kwargs):
                captured.update(kwargs)
                return {
                    "output": {"message": {"content": [{"text": "hi"}]}},
                    "usage": {"totalTokens": 3},
                }

        cfg = _config(model).llm
        with (
            patch("boto3.client", return_value=_Client()),
            patch(
                "turncall.services.aws_credentials.resolve_aws_credentials"
            ) as resolve,
        ):
            resolve.return_value.region = "us-east-1"
            resolve.return_value.access_key_id = "AKIA"
            resolve.return_value.secret_access_key = "s"
            resolve.return_value.session_token = None
            await llm_text._complete_text_bedrock(
                cfg, AWSConfig(), [{"role": "user", "content": "hi"}]
            )
        return captured

    async def test_claude_on_bedrock_is_sent_no_temperature(self) -> None:
        kwargs = await self._converse_kwargs("us.anthropic.claude-sonnet-5")

        assert "temperature" not in kwargs["inferenceConfig"]
        assert kwargs["inferenceConfig"]["maxTokens"] == 512

    async def test_another_vendor_on_bedrock_still_gets_one(self) -> None:
        kwargs = await self._converse_kwargs("meta.llama3-3-70b-instruct-v1:0")

        assert kwargs["inferenceConfig"]["temperature"] == 0.25
