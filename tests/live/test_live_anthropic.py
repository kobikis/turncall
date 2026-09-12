"""C2 — a real Anthropic call since the temperature fix.

#38 stopped sending `temperature` because current Claude models answer
`400 "temperature is deprecated for this model."` and the call died on its
first LLM turn. The fix is unit-tested at the construction seam and was probed
against the raw API — but nothing had run a completion through Pipecat's
service, which is what a call actually uses.
"""

import pytest

from turncall.domain.models import AgentConfig, LLMConfig

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_MODEL = "claude-haiku-4-5-20251001"


def _service(anthropic_key: str, model: str = _MODEL):
    from turncall.orchestrator.pipeline_factory import _create_llm_service

    return _create_llm_service(
        AgentConfig(llm=LLMConfig(provider="anthropic", model=model, max_tokens=32)),
        "",
        anthropic_api_key=anthropic_key,
        system_instruction="Answer in one word.",
    )


async def test_a_completion_runs_through_the_pipecat_service(
    anthropic_key: str,
) -> None:
    from pipecat.processors.aggregators.llm_context import LLMContext

    service = _service(anthropic_key)
    context = LLMContext(messages=[{"role": "user", "content": "Say hello."}])

    reply = await service.run_inference(context)

    assert reply and reply.strip(), "the Anthropic service returned nothing"


async def test_no_temperature_is_sent(anthropic_key: str) -> None:
    """The fix itself: a number reaching the provider is what broke every call."""
    service = _service(anthropic_key)

    given = service._settings.given_fields()

    assert not isinstance(given.get("temperature"), int | float), given


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-5"])
async def test_the_models_that_forced_the_fix_still_reject_one(
    anthropic_key: str, model: str
) -> None:
    """Pinned so the constraint is observed rather than remembered. If Anthropic
    un-deprecates temperature this fails, and the fix can be revisited."""
    import json
    import urllib.error
    import urllib.request

    body = {
        "model": model,
        "max_tokens": 8,
        "temperature": 0.7,
        "messages": [{"role": "user", "content": "hi"}],
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)  # noqa: S310

    assert caught.value.code == 400
    assert "temperature" in caught.value.read().decode()


class TestTheExtraEscapeHatch:
    """The way around the withholding rule has to actually work — it used to
    raise TypeError on the first LLM turn, which is worse than the bug #38
    fixed, because the docs sent people to it."""

    async def test_a_temperature_in_extra_completes(self, anthropic_key: str) -> None:
        from pipecat.processors.aggregators.llm_context import LLMContext

        service = _service(anthropic_key)
        service._settings.extra = _extra({"temperature": 0.25})

        reply = await service.run_inference(
            LLMContext(messages=[{"role": "user", "content": "Say hi."}])
        )

        assert reply and reply.strip()

    async def test_it_actually_reaches_the_model(self, anthropic_key: str) -> None:
        """A 200 on a model that tolerates temperature proves nothing — it
        could have been dropped silently. A model that *rejects* one is the
        only way to see it arrive."""
        from pipecat.processors.aggregators.llm_context import LLMContext

        service = _service(anthropic_key, model="claude-sonnet-5")
        service._settings.extra = _extra({"temperature": 0.25})

        with pytest.raises(Exception) as caught:
            await service.run_inference(
                LLMContext(messages=[{"role": "user", "content": "Say hi."}])
            )

        assert "temperature" in str(caught.value).lower(), caught.value


def _extra(raw: dict) -> dict:
    from turncall.orchestrator.pipeline_factory import _anthropic_extra_body

    return _anthropic_extra_body(raw)
