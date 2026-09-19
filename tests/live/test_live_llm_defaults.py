"""C10 — every LLM provider's house model is one that provider still serves.

`_LLM_DEFAULTS` names a model per provider, used whenever an agent sets none.
The unit tests can only assert the resolver returns what the table says; the
table is the part that was wrong — `gpt-4o-mini` sat there for all six
providers, and Anthropic answers `404 not_found_error` to it.

Model names also retire. A stale default breaks exactly those agents that
never named a model, which are the ones nobody is watching.
"""

import os

import httpx
import pytest

from turncall.services.llm_models import _LLM_DEFAULTS

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is not set")
    return value


async def test_openai_serves_its_house_model() -> None:
    key = _require("OPENAI_API_KEY")
    model = _LLM_DEFAULTS["openai"]

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "max_completion_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 200, f"{model}: {response.text[:200]}"


async def test_anthropic_serves_its_house_model() -> None:
    """The one the leak actually broke: this default replaces a model name
    Anthropic has never had."""
    key = _require("ANTHROPIC_API_KEY")
    model = _LLM_DEFAULTS["anthropic"]

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 200, f"{model}: {response.text[:200]}"


async def test_the_old_default_is_still_wrong_on_anthropic() -> None:
    """The control, and the reason the legacy value is read as unset: every
    agent created before this fix carries `gpt-4o-mini` in its config_blob.
    If Anthropic ever served a model by that name this would fail, and the
    sentinel handling could be dropped."""
    key = _require("ANTHROPIC_API_KEY")

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 404, response.text[:200]
    assert "not_found" in response.text
