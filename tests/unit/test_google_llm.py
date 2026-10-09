"""The `google` LLM provider (#205): Gemini as a cascade LLM, on the voice path.

The account already existed for Gemini Live S2S (ADR-0020); this is the same
key reaching a different service. What can go wrong is the usual three: the
wrong model when the agent named none, a sampling setting the model does
badly with, and a service the failover group (#222) cannot move off.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from pipecat.frames.frames import ErrorFrame, LLMContextFrame, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import LLMConfigSchema
from turncall.domain.models import AgentConfig, LLMConfig
from turncall.domain.providers import LLM_PROVIDERS, account_for, accounts
from turncall.orchestrator.llm_failover import FailoverLLM
from turncall.orchestrator.pipeline_factory import (
    _create_llm_service,
    _create_llm_stage,
)
from turncall.services.credentials import Credentials
from turncall.services.gemini_models import takes_temperature
from turncall.services.llm_models import model_is_required, resolve_llm_model

pytestmark = pytest.mark.unit


def _google(**llm: Any) -> AgentConfig:
    return AgentConfig(
        system_prompt="You are terse.", llm=LLMConfig(provider="google", **llm)
    )


class TestTheProvider:
    def test_is_an_llm_provider_on_the_existing_google_account(self) -> None:
        assert "google" in LLM_PROVIDERS
        assert account_for("google") == "google"
        # One key, two roles: the Gemini Live S2S one and now the cascade LLM.
        assert accounts()["google"] == {"llm": ["google"], "s2s": ["google"]}

    def test_has_a_house_model(self) -> None:
        assert not model_is_required("google")
        assert resolve_llm_model("google", "").startswith("gemini-")

    def test_heals_the_legacy_openai_sentinel(self) -> None:
        """`model_dump()` persisted `gpt-4o-mini` into old config blobs; on
        Google that is a 404 rather than a model."""
        assert resolve_llm_model("google", "gpt-4o-mini").startswith("gemini-")

    def test_a_chosen_model_passes_through(self) -> None:
        assert resolve_llm_model("google", "gemini-3.8-flash") == "gemini-3.8-flash"

    def test_the_schema_accepts_it_without_a_model(self) -> None:
        assert LLMConfigSchema(provider="google").model == ""

    def test_a_base_url_is_refused_rather_than_ignored(self) -> None:
        """The service is reached on Google's own endpoint; a base_url would
        silently do nothing."""
        with pytest.raises(ValidationError, match="base_url is not supported"):
            LLMConfigSchema(provider="google", base_url="https://gw.example/v1")


class TestTemperatureRule:
    """Google: "For all Gemini 3 models, we strongly recommend keeping the
    temperature parameter at its default value of 1.0" — below it they loop.
    TurnCall's 0.7 is its own default, persisted into every agent, so it is
    withheld there rather than sent for a knob nobody set."""

    @pytest.mark.parametrize(
        "model",
        [
            "gemini-3.6-flash",
            "gemini-3.8-flash",
            "gemini-3-flash-preview",
            "gemini-3.1-pro-preview",
            "gemini-3.5-flash-lite",
            "models/gemini-3.6-flash",
            # An unversioned alias points at Gemini 3 now.
            "gemini-flash-latest",
        ],
    )
    def test_gemini_3_and_aliases_get_none(self, model: str) -> None:
        assert not takes_temperature(model)

    @pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-1.5-pro"])
    def test_older_models_still_do(self, model: str) -> None:
        assert takes_temperature(model)


def _generation_params(service: Any) -> dict[str, Any]:
    """What pipecat actually sends: the request's generation config."""
    return service._build_generation_params(system_instruction="x")


class TestTheVoiceService:
    def test_builds_pipecats_google_service_on_the_house_model(self) -> None:
        from pipecat.services.google.llm import GoogleLLMService

        service = _create_llm_service(
            _google(max_tokens=222),
            creds(google="k"),
            system_instruction="You are terse.",
        )

        assert isinstance(service, GoogleLLMService)
        assert service._settings.model == resolve_llm_model("google", "")
        assert service._settings.system_instruction == "You are terse."
        assert _generation_params(service)["max_output_tokens"] == 222

    def test_inline_then_project_then_platform_key(self) -> None:
        project = {"google": {"api_key": "project-google"}}
        both = Credentials(platform={"google": "platform"}, project=project)
        only = Credentials(platform={"google": "platform"})

        assert _create_llm_service(_google(api_key="inline"), both)._api_key == (
            "inline"
        )
        assert _create_llm_service(_google(), both)._api_key == "project-google"
        assert _create_llm_service(_google(), only)._api_key == "platform"

    def test_gemini_3_is_sent_no_temperature(self) -> None:
        service = _create_llm_service(
            _google(model="gemini-3.6-flash", temperature=0.2), creds(google="k")
        )
        assert "temperature" not in _generation_params(service)

    def test_the_voicemail_pin_is_withheld_too(self) -> None:
        service = _create_llm_service(
            _google(model="gemini-3.6-flash"), creds(google="k"), temperature=0.1
        )
        assert "temperature" not in _generation_params(service)

    def test_an_older_model_gets_the_agents_temperature(self) -> None:
        service = _create_llm_service(
            _google(model="gemini-2.5-flash", temperature=0.2), creds(google="k")
        )
        assert _generation_params(service)["temperature"] == 0.2

    def test_extra_is_the_escape_hatch(self) -> None:
        """`llm.extra` merges into the generation config, last — so it can set
        a temperature on Gemini 3 anyway, or a thinking level."""
        service = _create_llm_service(
            _google(
                model="gemini-3.6-flash",
                extra={"temperature": 0.4, "thinkingConfig": {"thinkingLevel": "low"}},
            ),
            creds(google="k"),
        )
        params = _generation_params(service)
        assert params["temperature"] == 0.4
        assert params["thinkingConfig"] == {"thinkingLevel": "low"}

    def test_a_missing_key_is_reported_before_the_build(self) -> None:
        assert "google" in Credentials().missing_keys(_google())


class _FakeGenai:
    """The slice of google-genai's client pipecat's service calls."""

    def __init__(self, *turns: Any) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.aio = SimpleNamespace(
            models=SimpleNamespace(generate_content_stream=self._stream),
            aclose=self._aclose,
        )

    async def _stream(self, **request: Any) -> Any:
        self.requests.append(request)
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn

        async def chunks() -> Any:
            for chunk in turn:
                yield chunk

        return chunks()

    async def _aclose(self) -> None:
        pass


def _text_chunk(text: str) -> Any:
    from google.genai.types import GenerateContentResponse

    return GenerateContentResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finish_reason": "STOP",
                }
            ]
        }
    )


def _rejected(status: int) -> BaseException:
    """What google-genai raises for an HTTP error: a ClientError carrying the
    response, whose status is how pipecat classifies it (aiohttp's `status`)."""
    from google.genai.errors import ClientError

    body = {"error": {"code": status, "message": "API key not valid"}}
    return ClientError(status, body, SimpleNamespace(status=status))


class TestFailover:
    """#222: a group member like any other — as a backup and as the primary."""

    def test_a_google_backup_resolves_its_own_key(self) -> None:
        config = AgentConfig.model_validate(
            {"llm": {"failover": [{"provider": "google"}]}}
        )

        stage = _create_llm_stage(config, creds("sk-test", google="g-key"))

        assert isinstance(stage, FailoverLLM)
        assert stage.label(stage.llms[1]) == (
            f"google/{resolve_llm_model('google', '')}"
        )
        assert stage.llms[1]._api_key == "g-key"

    def test_a_keyless_google_backup_is_reported(self) -> None:
        config = AgentConfig.model_validate(
            {"llm": {"failover": [{"provider": "google"}]}}
        )
        assert "google" in creds("sk-test", google="").missing_keys(config)

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    async def test_an_unusable_gemini_fails_over_and_the_turn_is_answered(
        self, status: int
    ) -> None:
        google = _create_llm_service(_google(), creds(google="bad-key"))
        google._client = _FakeGenai(_rejected(status))
        backup = _create_llm_service(
            _google(model="gemini-3.8-flash"), creds(google="k")
        )
        backup._client = _FakeGenai([_text_chunk("Paris.")])
        group = FailoverLLM([("google/a", google), ("google/b", backup)])
        seen: list[tuple[str, str]] = []

        @group.event_handler("on_failover")
        async def _on(_g: object, src: str, dst: str, _e: ErrorFrame) -> None:
            seen.append((src, dst))

        down, _up = await run_test(
            group,
            frames_to_send=[
                LLMContextFrame(
                    context=LLMContext(
                        messages=[{"role": "user", "content": "Capital?"}]
                    )
                ),
                # The replayed turn is queued behind the failover; without a
                # pause the test's EndFrame closes the backup's client first.
                SleepFrame(sleep=0.2),
            ],
        )

        assert seen == [("google/a", "google/b")]
        assert [f.text for f in down if isinstance(f, LLMTextFrame)] == ["Paris."]

    async def test_a_rate_limit_does_not_switch(self) -> None:
        google = _create_llm_service(_google(), creds(google="k"))
        google._client = _FakeGenai(_rejected(429))
        backup = _create_llm_service(_google(), creds(google="k"))
        unused = _FakeGenai([_text_chunk("unused")])
        backup._client = unused
        group = FailoverLLM([("google/a", google), ("google/b", backup)])

        await run_test(
            group,
            frames_to_send=[LLMContextFrame(context=LLMContext(messages=[]))],
        )

        assert group.active_llm is google
        assert unused.requests == []
