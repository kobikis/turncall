"""Grok Voice as the `xai` S2S provider (#386, PRD #384).

What is checked here is what pipecat is handed: the service class, the model,
the voice, the instructions and the key. The socket itself is the live file's
(`tests/live/test_live_xai_realtime.py`).
"""

import inspect

import pytest
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.config.settings import Settings
from turncall.domain.models import AgentConfig, S2SConfig
from turncall.domain.providers import CREDENTIAL_ACCOUNTS, S2S_PROVIDERS, accounts
from turncall.orchestrator import s2s_config
from turncall.orchestrator.s2s_config import create_s2s_service
from turncall.services.credentials import Credentials, platform_keys

pytestmark = pytest.mark.unit


def _agent(**s2s) -> AgentConfig:
    return AgentConfig(
        name="grok",
        system_prompt="You are a receptionist.",
        pipeline_mode="s2s",
        s2s=S2SConfig(provider="xai", **s2s),
    )


def _service(config: AgentConfig | None = None, keys: Credentials | None = None):
    return create_s2s_service(config or _agent(), keys or creds(xai="xai-key"))


def _session(service):
    return service._settings.session_properties


class TestTheProviderExists:
    def test_an_agent_may_name_it(self) -> None:
        schema = AgentConfigSchema.model_validate(
            {"pipeline_mode": "s2s", "s2s": {"provider": "xai"}}
        )
        assert schema.s2s.provider == "xai"

    def test_an_inline_agent_may_name_it_too(self) -> None:
        """Call-init and eval targets validate through the same schema; the
        `INLINE_CONFIG` context is what they add."""
        from turncall.api.v1.schemas import INLINE_CONFIG

        schema = AgentConfigSchema.model_validate(
            {"pipeline_mode": "s2s", "s2s": {"provider": "xai"}},
            context=INLINE_CONFIG,
        )
        assert schema.s2s.provider == "xai"

    def test_an_unknown_provider_is_still_refused(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported S2S provider"):
            AgentConfigSchema.model_validate({"s2s": {"provider": "grok"}})

    def test_it_is_an_s2s_provider(self) -> None:
        assert "xai" in S2S_PROVIDERS

    def test_it_is_its_own_credential_account_serving_s2s_only(self) -> None:
        """So `PUT /v1/credentials/xai` is accepted and `/v1/providers` lists
        it, both of which read this namespace."""
        assert "xai" in CREDENTIAL_ACCOUNTS
        assert accounts()["xai"] == {"s2s": ["xai"]}

    def test_the_platform_key_is_read_from_its_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("XAI_API_KEY", "from-env")
        assert platform_keys(Settings())["xai"] == "from-env"


class TestTheServiceIsGrokRealtime:
    def test_pipecats_xai_module_not_the_deprecated_grok_one(self) -> None:
        from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService

        service = _service()

        assert isinstance(service, GrokRealtimeLLMService)
        # `pipecat.services.grok` re-exports the same class, so only the
        # import path tells the two apart.
        assert "pipecat.services.grok" not in inspect.getsource(s2s_config)
        assert service.base_url == "wss://api.x.ai/v1/realtime"

    def test_the_default_model_is_pinned_grok_voice_3(self) -> None:
        """Not `grok-voice-latest`: an alias would move a live agent's model
        underneath it when xAI moves the alias."""
        assert _service()._settings.model == "grok-voice-3.0"

    def test_the_default_voice_is_groks_not_openais(self) -> None:
        assert _session(_service()).voice == "eve"

    def test_an_explicit_model_is_passed_through_untouched(self) -> None:
        """ADR-0016's rule: a wrong value the agent actually chose is reported
        by the provider, not silently replaced."""
        service = _service(_agent(model="grok-voice-2.0"))
        assert service._settings.model == "grok-voice-2.0"

    def test_an_explicit_voice_is_passed_through_untouched(self) -> None:
        assert _session(_service(_agent(voice="rex"))).voice == "rex"

    def test_the_system_prompt_becomes_the_instructions(self) -> None:
        service = _service()
        assert _session(service).instructions == "You are a receptionist."
        assert service._settings.system_instruction == "You are a receptionist."

    def test_the_first_message_rides_in_the_instructions(self) -> None:
        """As on Realtime: the greeting run reads it from the instructions."""
        config = _agent().model_copy(update={"first_message": "Thanks for calling!"})
        assert "Thanks for calling!" in _session(_service(config)).instructions

    def test_audio_is_left_to_the_transport_rate(self) -> None:
        """pipecat fills PCM at the pipeline's rates in `setup()`; a format set
        here would win over the transport's 8 kHz or 16 kHz."""
        assert _session(_service()).audio is None

    def test_the_session_carries_no_server_side_tools(self) -> None:
        """xAI's own web/X/file search and hosted MCP run outside TurnCall's
        tool bridge; the agent's tools reach Grok through the context."""
        assert _session(_service()).tools is None


class TestTheKeyResolves:
    def test_project_credential_beats_the_platform_key(self) -> None:
        keys = Credentials(
            platform={"xai": "platform"}, project={"xai": {"api_key": "project"}}
        )
        assert _service(keys=keys).api_key == "project"

    def test_the_platform_key_is_the_fallback(self) -> None:
        keys = Credentials(platform={"xai": "platform"})
        assert _service(keys=keys).api_key == "platform"

    def test_an_openai_key_never_reaches_xai(self) -> None:
        """S2S gateway mode's flaw, which this provider exists to fix."""
        keys = Credentials(platform={"openai": "sk-openai", "xai": "xai-key"})
        assert _service(keys=keys).api_key == "xai-key"

    def test_no_key_anywhere_is_reported_before_the_call(self) -> None:
        keys = Credentials(platform={"openai": "sk-openai"})
        assert keys.missing_keys(_agent()) == ["xai"]

    def test_a_project_credential_satisfies_the_check(self) -> None:
        keys = Credentials(project={"xai": {"api_key": "p"}})
        assert keys.missing_keys(_agent()) == []


class TestARefusedKeyIsNamed:
    def test_the_service_reports_as_the_xai_provider(self) -> None:
        """`error.raised` names the vendor off the service's module, so a key
        xAI refuses mid-call carries `provider: "xai"` and `key_rejected`."""
        from turncall.orchestrator.call_session import provider_of

        assert provider_of(_service()) == "xai"

    @staticmethod
    def _handshake(status: int, body: bytes) -> Exception:
        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response

        return InvalidStatus(Response(status, "", Headers(), body))

    @staticmethod
    async def _category(exception: Exception):
        from unittest.mock import AsyncMock

        service = _service()
        service.push_frame = AsyncMock()
        await service.push_error("Error connecting to Grok", exception=exception)
        (frame,) = [c.args[0] for c in service.push_frame.await_args_list]
        return frame.category

    async def test_a_refused_key_is_an_authentication_error(self) -> None:
        """xAI answers a wrong key's handshake with a 400, not a 401 (measured:
        `{"code":"invalid-argument","error":"Incorrect API key provided..."}`),
        which pipecat would file as a bad request. `call_session` reads only
        AUTHENTICATION/AUTHORIZATION as a rejected key."""
        from pipecat.utils.errors import ErrorCategory

        refused = self._handshake(
            400,
            b'{"code":"invalid-argument","error":"Incorrect API key provided. '
            b'You can obtain an API key from https://console.x.ai."}',
        )
        assert await self._category(refused) is ErrorCategory.AUTHENTICATION

    async def test_a_401_stays_an_authentication_error(self) -> None:
        from pipecat.utils.errors import ErrorCategory

        refused = self._handshake(401, b"Unauthorized")
        assert await self._category(refused) is ErrorCategory.AUTHENTICATION

    async def test_any_other_bad_request_is_left_as_one(self) -> None:
        """An unknown model is the agent's mistake, not the key's."""
        from pipecat.utils.errors import ErrorCategory

        bad = self._handshake(400, b'{"error":"Model grok-voice-9 not found"}')
        assert await self._category(bad) is ErrorCategory.INVALID_REQUEST
