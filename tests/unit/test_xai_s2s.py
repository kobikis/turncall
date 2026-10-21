"""Grok Voice as the `xai` S2S provider (#386, PRD #384).

What is checked here is what pipecat is handed: the service class, the model,
the voice, the instructions and the key. The socket itself is the live file's
(`tests/live/test_live_xai_realtime.py`).
"""

import asyncio
import inspect
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

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

    def test_the_session_carries_no_server_side_tools(self) -> None:
        """xAI's own web/X/file search and hosted MCP run outside TurnCall's
        tool bridge; the agent's tools reach Grok through the context."""
        assert _session(_service()).tools is None


class TestGrokIsToldTheRateItIsSent:
    """The S2S pipeline resamples the caller to one service rate for every
    provider; Grok's session must declare that rate, not the pipeline's own
    (#389: 24 kHz audio declared as 16 kHz is noise to it)."""

    @pytest.mark.parametrize("transport_rate", [8000, 16000])
    def test_its_session_rates_are_the_resamplers(self, transport_rate) -> None:
        from types import SimpleNamespace

        from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService

        from turncall.orchestrator.audio_resampler import AudioResampler
        from turncall.orchestrator.pipeline_factory import create_pipeline

        pipeline = create_pipeline(
            config=_agent(),
            transport=MagicMock(),
            call_context=MagicMock(is_eval=True),
            pipecat_settings=SimpleNamespace(),
            credentials=creds(xai="xai-key"),
            audio_sample_rate=transport_rate,
        )
        resamplers = [p for p in pipeline.processors if isinstance(p, AudioResampler)]
        [grok] = [
            p
            for p in pipeline.processors_with_metrics()
            if isinstance(p, GrokRealtimeLLMService)
        ]
        # What pipecat's setup() does with the pipeline's rates: fill only
        # what the session left unset.
        grok._ensure_audio_config(transport_rate, transport_rate)

        assert len(resamplers) == 2
        for resampler in resamplers:
            assert grok._get_configured_sample_rate("input") == resampler._service_rate
            assert grok._get_configured_sample_rate("output") == resampler._service_rate
        assert _session(grok).audio.input.format.type == "audio/pcm"
        assert _session(grok).audio.output.format.type == "audio/pcm"


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


# --- #387: what Grok lacks is refused, what it has is applied ---------------


def _schema(s2s: dict, **agent) -> AgentConfigSchema:
    return AgentConfigSchema.model_validate(
        {"pipeline_mode": "s2s", "s2s": {"provider": "xai", **s2s}, **agent}
    )


def _inline_schema(s2s: dict) -> AgentConfigSchema:
    from turncall.api.v1.schemas import INLINE_CONFIG

    return AgentConfigSchema.model_validate(
        {"pipeline_mode": "s2s", "s2s": {"provider": "xai", **s2s}},
        context=INLINE_CONFIG,
    )


def _warnings(call) -> list[str]:
    """Loguru does not reach caplog; test_interruption_enabled.py's idiom."""
    from loguru import logger

    said: list[str] = []
    sink = logger.add(lambda m: said.append(str(m)), level="WARNING")
    try:
        call()
    finally:
        logger.remove(sink)
    return said


def _built_with_warnings(config: AgentConfig):
    built = []
    said = _warnings(lambda: built.append(_service(config)))
    return built[0], said


_LACKED = [
    ("base_url", "wss://gateway.example.com/v1/realtime"),
    ("temperature", 0.5),
    ("max_tokens", 256),
]


class TestWhatGrokLacksIsRefused:
    @pytest.mark.parametrize(("field", "value"), _LACKED)
    def test_a_stored_agent_is_a_422_naming_the_field(self, field, value) -> None:
        with pytest.raises(ValidationError, match=rf"s2s\.{field}.*xai"):
            _schema({field: value})

    @pytest.mark.parametrize(("field", "value"), _LACKED)
    def test_an_inline_agent_is_refused_the_same(self, field, value) -> None:
        with pytest.raises(ValidationError, match=rf"s2s\.{field}.*xai"):
            _inline_schema({field: value})

    @pytest.mark.parametrize(
        "url", ["wss://api.x.ai/v1/realtime", "https://gateway.example.com"]
    )
    def test_base_url_points_to_gateway_mode_on_openai(self, url) -> None:
        """Whatever the URL, the answer is the same: a gateway is S2S gateway
        mode on `openai`, so an xAI key is never sent to an arbitrary URL."""
        with pytest.raises(ValidationError) as refused:
            _schema({"base_url": url})
        message = str(refused.value)
        assert "S2S gateway mode" in message
        assert "provider 'openai'" in message
        # Not the generic validators' messages, which would hide the way out.
        assert "must start with ws://" not in message
        assert "only supported for the openai provider" not in message

    def test_gateway_mode_on_openai_is_untouched(self) -> None:
        url = "wss://api.x.ai/v1/realtime"
        schema = AgentConfigSchema.model_validate(
            {"pipeline_mode": "s2s", "s2s": {"provider": "openai", "base_url": url}}
        )
        assert schema.s2s.base_url == url

    @pytest.mark.parametrize(("field", "value"), _LACKED)
    def test_a_call_init_agent_fails_at_build_rather_than_dropping_it(
        self, field, value
    ) -> None:
        """A call-init inline agent is validated by the domain model, not the
        API schema, so the build applies the same rule (the Flux precedent)."""
        with pytest.raises(ValueError, match=rf"s2s\.{field}"):
            _service(_agent(**{field: value}))


class TestSessionExtras:
    def test_reasoning_reaches_the_session(self) -> None:
        session = _session(_service(_agent(extra={"reasoning": {"effort": "none"}})))
        assert session.reasoning.effort == "none"

    def test_replace_reaches_the_session(self) -> None:
        replace = {"TurnCall": "Turn Call"}
        assert _session(_service(_agent(extra={"replace": replace}))).replace == replace

    def test_resumption_reaches_the_session(self) -> None:
        session = _session(_service(_agent(extra={"resumption": {"enabled": True}})))
        assert session.resumption.enabled is True

    def test_all_three_go_out_on_the_session_update(self) -> None:
        """What xAI is sent, not only what the settings hold."""
        from pipecat.services.xai.realtime.events import SessionUpdateEvent

        extra = {
            "reasoning": {"effort": "high"},
            "replace": {"SKU": "S K U"},
            "resumption": {"enabled": True},
        }
        session = _session(_service(_agent(extra=extra)))
        wire = SessionUpdateEvent(session=session).model_dump(exclude_none=True)
        assert {k: wire["session"][k] for k in extra} == extra

    def test_known_keys_are_not_warned_about(self) -> None:
        _, said = _built_with_warnings(_agent(extra={"replace": {"a": "b"}}))
        assert not [m for m in said if "extra" in m], said

    def test_an_unknown_key_is_warned_about_and_dropped(self) -> None:
        service, said = _built_with_warnings(_agent(extra={"speed": 1.2}))
        assert any("xai" in m and "speed" in m for m in said), said
        assert "speed" not in _session(service).model_dump(exclude_none=True)

    def test_a_session_field_outside_the_three_is_no_back_door(self) -> None:
        """`voice` and `tools` are session properties too, but they have their
        own homes (s2s.voice, the agent's tools); extra cannot override them."""
        service, said = _built_with_warnings(
            _agent(extra={"voice": "rex", "tools": []})
        )
        assert _session(service).voice == "eve"
        assert _session(service).tools is None
        assert any("tools" in m and "voice" in m for m in said), said

    def test_an_unknown_key_is_accepted_at_save(self) -> None:
        """Logged at build and dropped, never a 422: a typo must not block a
        save any more than it kills a call (the stt.extra rule)."""
        assert _schema({"extra": {"speed": 1.2}}).s2s.extra == {"speed": 1.2}

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("reasoning", {"effort": "low"}),
            ("replace", {"word": 3}),
            ("resumption", "yes please"),
        ],
    )
    def test_a_bad_value_is_a_422_naming_the_key(self, key, value) -> None:
        with pytest.raises(ValidationError, match=rf"s2s\.extra\.{key}"):
            _schema({"extra": {key: value}})

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("reasoning", {"effort": "high"}),
            ("reasoning", {"effort": "none"}),
            ("reasoning", {"effort": "low"}),
            ("reasoning", {}),
            ("reasoning", None),
            ("reasoning", "high"),
            ("replace", {"Grok": "grock"}),
            ("replace", {"word": 3}),
            ("replace", ["Grok"]),
            ("resumption", {"enabled": True}),
            ("resumption", {"enabled": "yes please"}),
            ("resumption", "yes please"),
        ],
    )
    def test_the_api_and_pipecat_agree_on_a_value(self, key, value) -> None:
        """The API validates on TurnCall's copy of the three fields; the build
        on pipecat's. One verdict, or a saved agent could fail every call."""
        from pipecat.services.xai.realtime.events import SessionProperties

        from turncall.orchestrator import grok_voice

        try:
            SessionProperties.model_validate({key: value})
            pipecat_accepts = True
        except ValidationError:
            pipecat_accepts = False
        accepted = grok_voice._bad_extra({key: value}) is None
        assert accepted is pipecat_accepts

    def test_validating_an_agent_with_extras_loads_no_pipecat(self) -> None:
        """The API process validates agents on every write; pipecat's services
        are the call path's. A fresh interpreter, as the redaction import test
        does, measured past what validating any S2S agent already loads (the
        pronunciation validator's IPA helpers)."""
        import subprocess
        import sys
        import textwrap

        probe = textwrap.dedent(
            """
            import sys
            from turncall.api.v1.schemas.agents import AgentConfigSchema
            from turncall.domain.models import S2SConfig
            from turncall.orchestrator import grok_voice
            AgentConfigSchema.model_validate(
                {"pipeline_mode": "s2s", "s2s": {"provider": "openai"}}
            )
            before = {m for m in sys.modules if m.split(".")[0] == "pipecat"}
            assert not [m for m in before if m.startswith("pipecat.services")]
            extra = {
                "reasoning": {"effort": "none"},
                "replace": {"Grok": "grock"},
                "resumption": {"enabled": True},
            }
            AgentConfigSchema.model_validate(
                {"pipeline_mode": "s2s", "s2s": {"provider": "xai", "extra": extra}}
            )
            assert grok_voice.refusal(S2SConfig(provider="xai", extra=extra)) is None
            try:
                AgentConfigSchema.model_validate({"pipeline_mode": "s2s",
                    "s2s": {"provider": "xai", "extra": {"reasoning": "low"}}})
            except ValueError:
                pass
            else:
                raise SystemExit("a bad value was accepted")
            loaded = sorted(
                m for m in sys.modules
                if m.split(".")[0] == "pipecat" and m not in before
            )
            print("LOADED=" + ",".join(loaded))
            """
        )
        result = subprocess.run(  # noqa: S603 - our own interpreter and code
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        [loaded] = [x for x in result.stdout.splitlines() if x.startswith("LOADED=")]
        assert loaded == "LOADED=", loaded

    def test_a_bad_value_on_a_call_init_agent_fails_at_build(self) -> None:
        with pytest.raises(ValueError, match=r"s2s\.extra\.reasoning"):
            _service(_agent(extra={"reasoning": {"effort": "low"}}))


class TestTurnDetection:
    def test_server_vad_is_groks_own(self) -> None:
        service = _service()
        assert _session(service).turn_detection.type == "server_vad"
        # pipecat then recommends Grok's turn frames to the aggregator.
        assert service.service_metadata_frame().user_turn_strategies is not None

    def test_pipecat_vad_turns_server_detection_off(self) -> None:
        """pipecat's manual mode: the pipeline's VAD ends the turn, and the
        service commits the buffer and asks for a response itself."""
        service = _service(_agent(turn_detection="pipecat_vad"))
        assert _session(service).turn_detection is None
        assert service.service_metadata_frame().user_turn_strategies is None


class TestTheS2SRulesHold:
    @pytest.mark.parametrize(
        ("block", "entry"),
        [
            ("stt", {"provider": "openai"}),
            ("tts", {"provider": "openai", "voice": "alloy"}),
            ("llm", {"provider": "anthropic"}),
        ],
    )
    def test_failover_is_refused(self, block, entry) -> None:
        with pytest.raises(ValidationError, match=rf"{block}\.failover"):
            _schema({}, **{block: {"failover": [entry]}})

    def test_delegation_is_refused(self) -> None:
        with pytest.raises(ValidationError, match=r"s2s\.delegation"):
            _schema({"delegation": {"mode": "client"}})

    def test_pronunciations_and_barge_in_settings_are_warned_about(self) -> None:
        from tests.unit.test_tts_pronunciations import METFORMIN
        from turncall.domain.models import TTSConfig
        from turncall.orchestrator.pipeline_factory import _create_s2s_pipeline

        config = _agent().model_copy(
            update={
                "tts": TTSConfig(pronunciations=METFORMIN),
                "interruption_enabled": False,
                "greeting_interruptible": False,
            }
        )
        said = _warnings(
            lambda: _create_s2s_pipeline(
                config=config,
                transport=MagicMock(),
                call_context=MagicMock(),
                pipecat_settings=MagicMock(),
                credentials=creds(xai="xai-key"),
            )
        )
        assert any("tts.pronunciations is ignored on S2S" in m for m in said), said
        assert any("interruption_enabled=false is not applied" in m for m in said)
        assert any("greeting_interruptible=false is not applied" in m for m in said)


class TestTheSessionToolsAreTheAgents:
    """xAI's own server-side tools (`web_search`, `x_search`, `file_search`,
    hosted `mcp`) run outside TurnCall's tool bridge — unrecorded, unmockable,
    unredacted — so none is ever in the session (#388). What is checked is the
    `session.update` pipecat sends for an agent the factory built with every
    kind of tool TurnCall has, not only the service's settings."""

    _EMPTY: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def _wire_tools(
        self, *, with_tools: bool, extra: dict[str, Any] | None = None
    ) -> list[dict]:
        """The `tools` of the `session.update` pipecat sends for an `xai`
        agent built by the factory — with a webhook tool, a built-in, an MCP
        tool and a `tool`-mode KB, or with none at all (where pipecat falls
        back to the session's own tools)."""
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMUserAggregator,
        )
        from pipecat.services.xai.realtime.events import SessionUpdateEvent
        from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService

        from turncall.domain.models import ToolDefinition
        from turncall.orchestrator.pipeline_factory import create_pipeline

        tools = [
            {
                "name": "book_table",
                "description": "Book a table.",
                "parameters_schema": self._EMPTY,
                "webhook_url": "https://example.com/book",
            },
            {
                "name": "end_call",
                "description": "Hang up.",
                "parameters_schema": self._EMPTY,
            },
        ]
        crm = ToolDefinition(
            name="crm_lookup",
            description="Look up a caller.",
            parameters_schema=self._EMPTY,
        )
        kb = {
            "knowledge_base_id": uuid4(),
            "mode": "tool",
            "top_k": 3,
            "similarity_threshold": 0.3,
            "tool_description": None,
        }
        config = AgentConfig.model_validate(
            {
                "pipeline_mode": "s2s",
                "system_prompt": "You are a receptionist.",
                "s2s": {"provider": "xai", "extra": extra or {}},
                "tools": tools if with_tools else [],
            }
        )
        pipeline = create_pipeline(
            config=config,
            transport=MagicMock(),
            call_context=MagicMock(is_eval=False, records=False, has_database=True),
            pipecat_settings=MagicMock(),
            credentials=creds("sk-test", xai="xai-key"),
            mcp_tools=[crm] if with_tools else None,
            knowledge_base_attachments=[kb] if with_tools else None,
        )
        grok = next(
            p for p in pipeline.processors if isinstance(p, GrokRealtimeLLMService)
        )
        user = next(p for p in pipeline.processors if isinstance(p, LLMUserAggregator))
        grok._context = user.context
        grok.send_client_event = AsyncMock()
        asyncio.run(grok._send_session_update())
        (event,) = [c.args[0] for c in grok.send_client_event.await_args_list]
        assert isinstance(event, SessionUpdateEvent)
        return event.model_dump(exclude_none=True)["session"].get("tools", [])

    def test_only_the_agents_function_tools_are_sent(self) -> None:
        tools = self._wire_tools(with_tools=True)

        assert {t["type"] for t in tools} == {"function"}
        assert {t["name"] for t in tools} == {
            "book_table",
            "end_call",
            "crm_lookup",
            "query_knowledge",
        }

    def test_an_agent_without_tools_sends_none(self) -> None:
        """Where the context has no tools pipecat sends the session's own, so
        this is where one set at build would reach xAI."""
        assert self._wire_tools(with_tools=False) == []

    def test_s2s_extra_cannot_add_one(self) -> None:
        """`tools` in `s2s.extra` is dropped, like every key outside the
        three the session takes from it."""
        extra = {"tools": [{"type": "web_search"}, {"type": "x_search"}]}

        assert self._wire_tools(with_tools=False, extra=extra) == []
