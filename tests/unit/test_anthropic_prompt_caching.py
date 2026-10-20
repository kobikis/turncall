"""Claude agents cache their system prompt and tools (#374).

Without a cache breakpoint every turn of a call re-sent — and paid full input
price and full time-to-first-token for — the same system prompt and tool list.
pipecat 1.13's `enable_prompt_caching` puts one on the system prompt (and on
the latest user turns), so the prefix is read back from Anthropic's cache.

The TTL is Anthropic's default, 5 minutes: a call's turns come seconds apart,
and every read refreshes the entry. ADR-0028's addendum has the measurement and
why not "1h".
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import creds
from turncall.domain.models import AgentConfig, LLMConfig
from turncall.services.llm_text import complete_text


def _anthropic_agent(**llm: object) -> AgentConfig:
    return AgentConfig(
        llm=LLMConfig(provider="anthropic", model="claude-sonnet-5", **llm)
    )


@pytest.mark.unit
class TestVoice:
    def test_the_service_caches(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        service = _create_llm_service(
            _anthropic_agent(), creds("", anthropic="k"), system_instruction="Be kind."
        )

        assert service._settings.enable_prompt_caching is True
        # Anthropic's default, 5 minutes — not the 1-hour entry at twice the
        # write price.
        assert service._settings.system_prompt_cache_ttl is None

    @pytest.mark.asyncio
    async def test_the_request_carries_a_breakpoint_on_the_system_prompt(
        self,
    ) -> None:
        """What reaches Anthropic, not just the flag: the system prompt goes as
        one text block with `cache_control`, so it and the tools ahead of it
        are cached."""
        from pipecat.processors.aggregators.llm_context import LLMContext

        from turncall.orchestrator.pipeline_factory import _create_llm_service

        service = _create_llm_service(
            _anthropic_agent(), creds("", anthropic="k"), system_instruction="Be kind."
        )
        params = await service._get_llm_invocation_params(
            LLMContext(messages=[{"role": "user", "content": "hi"}])
        )

        assert params["system"] == [
            {
                "type": "text",
                "text": "Be kind.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_every_failover_member_caches(self) -> None:
        """A Claude backup — and an `openai_live` client delegate, which is
        built by the same `_create_llm_stage` — caches like the primary."""
        from turncall.orchestrator.pipeline_factory import _create_llm_stage

        config = _anthropic_agent(
            failover=[LLMConfig(provider="anthropic", model="claude-haiku-4-5")]
        )
        group = _create_llm_stage(config, creds("", anthropic="k"))

        assert [s._settings.enable_prompt_caching for s in group.services] == [
            True,
            True,
        ]


def _client(payload: dict) -> MagicMock:
    client = AsyncMock()
    response = MagicMock()
    response.json.return_value = payload
    client.post.return_value = response
    return client


_REPLY = {
    "content": [{"type": "text", "text": "hi"}],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 4,
        "output_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1500,
    },
}


@pytest.mark.unit
@pytest.mark.asyncio
class TestText:
    async def test_the_system_prompt_is_one_cached_block(self) -> None:
        """Chat and SMS cache the same prefix the voice path does."""
        client = _client(_REPLY)
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            await complete_text(
                LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="k"),
                [
                    {"role": "system", "content": "Be kind."},
                    {"role": "user", "content": "hello"},
                ],
            )

        assert client.post.call_args.kwargs["json"]["system"] == [
            {
                "type": "text",
                "text": "Be kind.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_no_system_prompt_sends_no_system(self) -> None:
        client = _client(_REPLY)
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            await complete_text(
                LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="k"),
                [{"role": "user", "content": "hello"}],
            )

        assert "system" not in client.post.call_args.kwargs["json"]

    async def test_cached_tokens_still_count(self) -> None:
        """Anthropic reports `input_tokens` net of the cache, so a cached turn
        would otherwise store a token count of a handful."""
        client = _client(_REPLY)
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            out = await complete_text(
                LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="k"),
                [{"role": "user", "content": "hello"}],
            )

        assert out.total_tokens == 4 + 2 + 1500
