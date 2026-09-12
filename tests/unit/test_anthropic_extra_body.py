"""`llm.extra` on Anthropic has to reach the model, not raise.

CLAUDE.md told people to use `llm.extra` to set a temperature on a Claude model
that still accepts one — the documented way around the withholding rule from
#38. It did not work: the Anthropic SDK dropped `temperature`, `top_k` and
`top_p` from `messages.create()`, so passing one by name raised
`TypeError: AsyncMessages.create() got an unexpected keyword argument
'temperature'` on the first LLM turn of the call. The documented escape hatch
crashed the thing it was supposed to rescue.

Keys the SDK won't take by name now travel in `extra_body`, checked against its
own signature so this survives the next release rather than pinning a list.
"""

import pytest

from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator.pipeline_factory import _anthropic_extra_body


@pytest.mark.unit
class TestRouting:
    def test_the_documented_form_is_made_to_work(self) -> None:
        assert _anthropic_extra_body({"temperature": 0.25}) == {
            "extra_body": {"temperature": 0.25}
        }

    def test_an_explicit_extra_body_is_left_alone(self) -> None:
        assert _anthropic_extra_body({"extra_body": {"temperature": 0.25}}) == {
            "extra_body": {"temperature": 0.25}
        }

    def test_the_two_forms_merge_rather_than_one_winning(self) -> None:
        """Someone who writes both meant both."""
        assert _anthropic_extra_body(
            {"top_k": 5, "extra_body": {"temperature": 0.25}}
        ) == {"extra_body": {"temperature": 0.25, "top_k": 5}}

    def test_a_key_the_sdk_still_accepts_stays_where_it_was(self) -> None:
        """Only the ones that would raise get moved — `extra_body` is not a
        dumping ground."""
        assert _anthropic_extra_body({"stop_sequences": ["x"]}) == {
            "stop_sequences": ["x"]
        }

    def test_nothing_in_means_nothing_out(self) -> None:
        assert _anthropic_extra_body({}) == {}

    def test_the_split_follows_the_sdk_rather_than_a_hardcoded_list(self) -> None:
        """If a future SDK restores `temperature` to the signature, this stops
        wrapping it — without anyone editing a list."""
        import inspect

        from anthropic.resources.messages import AsyncMessages

        accepted = set(inspect.signature(AsyncMessages.create).parameters)
        routed = _anthropic_extra_body({"temperature": 0.25, "stop_sequences": ["x"]})

        for key in routed.get("extra_body", {}):
            assert key not in accepted, f"{key} was wrapped but create() takes it"


@pytest.mark.unit
class TestTheServiceGetsIt:
    def test_a_temperature_in_extra_survives_to_the_settings(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        service = _create_llm_service(
            AgentConfig(
                llm=LLMConfig(
                    provider="anthropic",
                    model="claude-haiku-4-5",
                    extra={"temperature": 0.25},
                )
            ),
            "",
            anthropic_api_key="k",
        )

        assert service._settings.extra == {"extra_body": {"temperature": 0.25}}

    def test_an_agent_that_sets_no_extra_sends_none(self) -> None:
        """The #38 rule is unchanged: no temperature unless asked for."""
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        service = _create_llm_service(
            AgentConfig(llm=LLMConfig(provider="anthropic", model="claude-sonnet-5")),
            "",
            anthropic_api_key="k",
        )

        given = service._settings.given_fields()
        assert not isinstance(given.get("temperature"), int | float)
