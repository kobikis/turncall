"""Grok as an LLM provider: `llm.provider: "xai"` (#393).

Grok's text models on the xAI account (agent, then project, then platform),
on the voice pipeline, the text path and inside `llm.failover`. A key xAI
refuses is HTTP 400 there too (measured, #392), read as one by the
`XAIKeyRefusal` mixin.
"""

import pytest

from tests.conftest import creds
from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator.pipeline_factory import _create_llm_service

HOUSE_MODEL = "grok-4.20-non-reasoning"
# What xAI answers a wrong key with, on every endpoint (measured, #392).
WRONG_KEY = {
    "code": "Client specified an invalid argument",
    "error": "Incorrect API key provided: xa***. You can obtain an API key "
    "from https://console.x.ai.",
}
TOOLS = [{"name": "lookup", "description": "Look up", "parameters": {}}]


def _voice(llm: dict, **keys: str):
    config = AgentConfig.model_validate({"llm": {"provider": "xai", **llm}})
    return _create_llm_service(config, creds("sk-openai", **keys))


@pytest.mark.unit
class TestTheVoiceServiceIsGrok:
    def test_pipecats_xai_module_not_the_deprecated_grok_one(self) -> None:
        from pipecat.services.xai.llm import GrokLLMService

        service = _voice({}, xai="xai-platform")

        assert isinstance(service, GrokLLMService)
        assert type(service).__module__.startswith("turncall.orchestrator")

    def test_an_unnamed_model_is_the_house_model(self) -> None:
        assert _voice({}, xai="k")._settings.model == HOUSE_MODEL

    def test_the_legacy_openai_sentinel_reads_as_unset(self) -> None:
        assert _voice({"model": "gpt-4o-mini"}, xai="k")._settings.model == HOUSE_MODEL

    def test_a_named_model_is_sent_as_named(self) -> None:
        assert _voice({"model": "grok-4.3"}, xai="k")._settings.model == "grok-4.3"

    def test_it_talks_to_xai_with_the_xai_key(self) -> None:
        service = _voice({}, xai="xai-platform")

        assert service._client.api_key == "xai-platform"
        assert str(service._client.base_url).startswith("https://api.x.ai/v1")

    def test_the_projects_key_beats_the_platforms(self) -> None:
        from turncall.services.credentials import Credentials

        config = AgentConfig.model_validate({"llm": {"provider": "xai"}})
        keys = Credentials(
            platform={"xai": "xai-platform"},
            project={"xai": {"api_key": "xai-project"}},
        )

        assert _create_llm_service(config, keys)._client.api_key == "xai-project"

    def test_the_agents_own_key_wins(self) -> None:
        service = _voice({"api_key": "xai-agent"}, xai="xai-platform")

        assert service._client.api_key == "xai-agent"


@pytest.mark.unit
class TestReasoningEffort:
    def test_rides_on_pipecats_setting(self) -> None:
        """Through Settings, not `extra_body`, so the two never compete."""
        service = _voice({"model": "grok-4.3", "reasoning_effort": "low"}, xai="k")

        assert service._settings.reasoning_effort == "low"
        assert "reasoning_effort" not in (service._settings.extra or {}).get(
            "extra_body", {}
        )

    def test_unset_sends_none(self) -> None:
        """A non-reasoning model rejects the parameter outright."""
        assert _voice({}, xai="k")._settings.reasoning_effort is None

    def test_the_voicemail_classifier_forces_it_off(self) -> None:
        config = AgentConfig.model_validate(
            {"llm": {"provider": "xai", "reasoning_effort": "high"}}
        )
        service = _create_llm_service(
            config, creds(xai="k"), temperature=0.1, reasoning_effort=None
        )

        assert service._settings.reasoning_effort is None


@pytest.mark.unit
class TestOnAVoiceCall:
    def test_a_failover_member_runs_on_its_own_xai_key(self) -> None:
        from turncall.orchestrator.llm_failover import FailoverLLM
        from turncall.orchestrator.pipeline_factory import _create_llm_stage

        config = AgentConfig.model_validate(
            {"llm": {"failover": [{"provider": "xai"}]}}
        )
        stage = _create_llm_stage(config, creds("sk-openai", xai="xai-platform"))

        assert isinstance(stage, FailoverLLM)
        assert stage.label(stage.llms[1]) == f"xai/{HOUSE_MODEL}"
        assert stage.llms[1]._client.api_key == "xai-platform"

    def test_a_missing_xai_key_is_reported(self) -> None:
        config = AgentConfig.model_validate({"llm": {"provider": "xai"}})

        assert "xai" in creds("sk-openai", xai="").missing_keys(config)

    async def test_warm_up_reaches_grok(self) -> None:
        from unittest.mock import AsyncMock

        from turncall.orchestrator.llm_warm_up import warm_up

        service = _voice({}, xai="k")
        create = AsyncMock()
        service._client.chat.completions.create = create

        await warm_up(service)

        assert create.call_args.kwargs["model"] == HOUSE_MODEL


def _sdk_error(status: int, body: dict) -> Exception:
    """What the OpenAI SDK raises when api.x.ai answers a chat completion
    with this status: the JSON body on `.body`, not in its message."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.x.ai/v1/chat/completions")
    response = httpx.Response(status, request=request, json=body)
    cls = {400: openai.BadRequestError, 403: openai.PermissionDeniedError}[status]
    return cls("Error code", response=response, body=body)


async def _complete_through_pipecat(service, error: Exception):
    """Run one completion through pipecat's own `process_frame`, the client
    raising `error`, and return the ErrorFrame it pushed."""
    from unittest.mock import AsyncMock

    from pipecat.frames.frames import ErrorFrame, LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection

    service._client.chat.completions.create = AsyncMock(side_effect=error)
    service.push_frame = AsyncMock()
    await service.process_frame(
        LLMContextFrame(context=LLMContext()), FrameDirection.DOWNSTREAM
    )
    pushed = [c.args[0] for c in service.push_frame.await_args_list]
    (frame,) = [f for f in pushed if isinstance(f, ErrorFrame)]
    return frame


@pytest.mark.unit
class TestARefusedKeyIsNamed:
    """`call_session` reads AUTHENTICATION/AUTHORIZATION from an unusable
    service as `key_rejected`, naming `provider_of(service)`."""

    def test_the_service_reports_as_the_xai_provider(self) -> None:
        from turncall.orchestrator.call_session import provider_of

        assert provider_of(_voice({}, xai="k")) == "xai"

    async def test_a_wrong_key_is_authentication_and_ends_the_service(self) -> None:
        """xAI answers a wrong key with 400 (measured), and pipecat's OpenAI
        service hands the SDK error itself to `push_error`, so the mixin
        sees its body."""
        from pipecat.utils.errors import ErrorCategory

        service = _voice({}, xai="wrong")
        frame = await _complete_through_pipecat(service, _sdk_error(400, WRONG_KEY))

        assert frame.category is ErrorCategory.AUTHENTICATION
        assert not service.is_usable

    async def test_a_voice_only_key_is_authorization(self) -> None:
        """A key without the chat endpoint answers 403 (measured), which
        pipecat already files as AUTHORIZATION."""
        from pipecat.utils.errors import ErrorCategory

        denied = {"code": "permission-denied", "error": "no access to chat"}
        frame = await _complete_through_pipecat(
            _voice({}, xai="voice-only"), _sdk_error(403, denied)
        )

        assert frame.category is ErrorCategory.AUTHORIZATION

    async def test_an_unknown_model_is_left_a_bad_request(self) -> None:
        from pipecat.utils.errors import ErrorCategory

        frame = await _complete_through_pipecat(
            _voice({"model": "grok-9"}, xai="k"),
            _sdk_error(400, {"error": "Model grok-9 not found"}),
        )

        assert frame.category is ErrorCategory.INVALID_REQUEST


@pytest.mark.unit
class TestATextTurnGoesToXai:
    """Chat, SMS, WhatsApp text, post-call analysis, takeaways and the
    transfer briefing all run on `complete_text`, whose base URL used to
    fall back to OpenAI's: the xAI key would have gone to OpenAI."""

    @staticmethod
    async def _post(config: LLMConfig, **kw):
        from unittest.mock import AsyncMock, MagicMock, patch

        from turncall.services.credentials import Credentials
        from turncall.services.llm_text import complete_text

        reply = MagicMock()
        reply.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"total_tokens": 3},
        }
        client = AsyncMock()
        client.post.return_value = reply
        keys = Credentials(
            platform={"openai": "sk-openai", "xai": "xai-platform"},
            project={"xai": {"api_key": "xai-project"}},
        )
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            out = await complete_text(
                config, [{"role": "user", "content": "hi"}], credentials=keys, **kw
            )
        assert out.text == "hi"
        return client.post.await_args

    async def test_to_api_x_ai_with_the_projects_xai_key(self) -> None:
        call = await self._post(LLMConfig(provider="xai"))

        assert call.args[0] == "https://api.x.ai/v1/chat/completions"
        assert call.kwargs["headers"]["Authorization"] == "Bearer xai-project"
        assert call.kwargs["json"]["model"] == HOUSE_MODEL

    async def test_tools_ride_along(self) -> None:
        from unittest.mock import AsyncMock

        call = await self._post(
            LLMConfig(provider="xai"), tools=TOOLS, execute_tool=AsyncMock()
        )

        assert call.kwargs["json"]["tools"][0]["function"]["name"] == "lookup"

    async def test_a_stored_base_url_never_takes_the_key_elsewhere(self) -> None:
        """The API refuses one, but a stored or inline config is not re-read
        through that schema."""
        call = await self._post(
            LLMConfig(provider="xai", base_url="https://elsewhere.example/v1")
        )

        assert call.args[0] == "https://api.x.ai/v1/chat/completions"


@pytest.mark.unit
class TestTheBoundary:
    @staticmethod
    def _schema(**llm):
        from turncall.api.v1.schemas.agents import AgentConfigSchema

        return AgentConfigSchema.model_validate({"llm": {"provider": "xai", **llm}})

    def test_an_xai_agent_needs_no_model(self) -> None:
        assert self._schema().llm.provider == "xai"

    def test_a_base_url_is_refused(self) -> None:
        """It would carry the project's xAI key to an arbitrary URL."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="base_url is not supported for th"):
            self._schema(base_url="https://example.com/v1")

    def test_minimal_effort_is_refused(self) -> None:
        """OpenAI's lowest effort, not one of xAI's."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="minimal"):
            self._schema(reasoning_effort="minimal")

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_xais_efforts_are_accepted(self, effort: str) -> None:
        assert self._schema(reasoning_effort=effort).llm.reasoning_effort == effort

    def test_it_is_a_failover_member(self) -> None:
        from turncall.api.v1.schemas.agents import AgentConfigSchema

        schema = AgentConfigSchema.model_validate(
            {"llm": {"provider": "openai", "failover": [{"provider": "xai"}]}}
        )

        assert schema.llm.failover[0].provider == "xai"

    def test_a_failover_member_is_held_to_the_same_rules(self) -> None:
        from pydantic import ValidationError

        from turncall.api.v1.schemas.agents import AgentConfigSchema

        with pytest.raises(ValidationError, match="base_url"):
            AgentConfigSchema.model_validate(
                {"llm": {"failover": [{"provider": "xai", "base_url": "https://e.x"}]}}
            )
