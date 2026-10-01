"""Project credentials (ADR-0020, #161-#164).

Mirrors test_aws_bedrock.py: resolution, the value reaching the constructed
service, rejection at the API boundary, and no read returning a value.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from turncall.api.deps import get_session
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.config.settings import Settings, get_settings
from turncall.domain.enums import ProjectRole
from turncall.domain.models import (
    AgentConfig,
    AvatarConfig,
    AWSConfig,
    LLMConfig,
    S2SConfig,
    STTConfig,
    TTSConfig,
)
from turncall.domain.providers import ROLE_PROVIDERS, account_for, accounts
from turncall.orchestrator import pipeline_factory as pf
from turncall.orchestrator.s2s_config import create_s2s_service
from turncall.services.credentials import Credentials
from turncall.storage.repositories import credential_repo

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
REPO = "turncall.storage.repositories.credential_repo"
SECRET = "sk-very-secret-value"


def _creds(project=None, **platform) -> Credentials:
    return Credentials(platform=platform, project=project or {})


class TestCredentialResolution:
    def test_inline_beats_project_beats_platform(self) -> None:
        c = _creds({"openai": {"api_key": "project"}}, openai="platform")
        assert c.api_key("openai", "inline") == "inline"
        assert c.api_key("openai") == "project"
        assert _creds(openai="platform").api_key("openai") == "platform"

    def test_no_level_holds_one(self) -> None:
        assert _creds().api_key("deepgram") == ""

    def test_one_account_serves_every_role(self) -> None:
        served = accounts()
        assert set(served["deepgram"]) == {"stt", "tts"}
        assert set(served["openai"]) == {"stt", "llm", "tts", "s2s"}
        assert served["openai"]["s2s"] == ["openai", "openai_live"]
        assert served["aws"] == {"llm": ["bedrock"], "s2s": ["aws"]}
        assert account_for("ollama") is None
        assert account_for("custom_openai") is None

    def test_aws_inline_source_wins_whole(self) -> None:
        inline = AWSConfig(role_arn="arn:inline")
        c = _creds({"aws": {"access_key_id": "AKIA", "secret_access_key": "s"}})
        assert c.aws(inline) is inline

    def test_aws_project_set_used_when_agent_names_no_source(self) -> None:
        c = _creds(
            {
                "aws": {
                    "access_key_id": "AKIAPROJ",
                    "secret_access_key": "s",
                    "region": "eu-west-1",
                }
            }
        )
        resolved = c.aws(AWSConfig())
        assert resolved.access_key_id == "AKIAPROJ"
        assert resolved.region == "eu-west-1"
        # The agent's region is the agent's choice; model availability is regional.
        assert c.aws(AWSConfig(region="us-west-2")).region == "us-west-2"

    def test_aws_without_any_level_is_the_ambient_chain(self) -> None:
        inline = AWSConfig(region="us-east-1")
        assert _creds().aws(inline) is inline


def _ctor(monkeypatch: pytest.MonkeyPatch, module: str, name: str) -> MagicMock:
    import importlib

    mock = MagicMock(name=name)
    monkeypatch.setattr(importlib.import_module(module), name, mock)
    return mock


def _key(mock: MagicMock) -> str:
    return mock.call_args.kwargs["api_key"]


PROJECT = {
    name: {"api_key": f"project-{name}"}
    for name in (
        "openai",
        "anthropic",
        "openrouter",
        "google",
        "deepgram",
        "elevenlabs",
        "cartesia",
        "heygen",
        "tavus",
    )
}


class TestCredentialsReachTheServices:
    @pytest.fixture(autouse=True)
    def _no_aiohttp_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The shared session needs a running loop; the constructors are mocked.
        monkeypatch.setattr(
            "turncall.adapters.aiohttp_client.get_aiohttp_session", MagicMock
        )

    @pytest.mark.parametrize(
        ("provider", "module", "cls"),
        [
            ("deepgram", "pipecat.services.deepgram.stt", "DeepgramSTTService"),
            ("elevenlabs", "pipecat.services.elevenlabs.stt", "ElevenLabsSTTService"),
            ("openai", "pipecat.services.openai.stt", "OpenAISTTService"),
            ("cartesia", "pipecat.services.cartesia.stt", "CartesiaSTTService"),
        ],
    )
    def test_stt(self, monkeypatch, provider, module, cls) -> None:
        mock = _ctor(monkeypatch, module, cls)
        config = AgentConfig(stt=STTConfig(provider=provider))
        pf._create_stt_service(config, _creds(PROJECT, **{provider: "platform"}))
        assert _key(mock) == f"project-{provider}"
        pf._create_stt_service(config, _creds(**{provider: "platform"}))
        assert _key(mock) == "platform"

    @pytest.mark.parametrize(
        ("provider", "module", "cls"),
        [
            ("deepgram", "pipecat.services.deepgram.tts", "DeepgramTTSService"),
            ("elevenlabs", "pipecat.services.elevenlabs.tts", "ElevenLabsTTSService"),
            ("openai", "pipecat.services.openai.tts", "OpenAITTSService"),
            ("cartesia", "pipecat.services.cartesia.tts", "CartesiaTTSService"),
        ],
    )
    def test_tts(self, monkeypatch, provider, module, cls) -> None:
        mock = _ctor(monkeypatch, module, cls)
        config = AgentConfig(tts=TTSConfig(provider=provider, voice="v"))
        pf._create_tts_service(config, _creds(PROJECT, **{provider: "platform"}))
        assert _key(mock) == f"project-{provider}"
        pf._create_tts_service(config, _creds(**{provider: "platform"}))
        assert _key(mock) == "platform"

    def test_cartesia_with_no_key_anywhere_names_both_fixes(self) -> None:
        config = AgentConfig(tts=TTSConfig(provider="cartesia", voice="v"))
        with pytest.raises(ValueError, match=r"project credential.*CARTESIA_API_KEY"):
            pf._create_tts_service(config, _creds())

    def test_openai_llm_honours_inline_key(self) -> None:
        """The #161 bug: a key set on an OpenAI agent used to be discarded."""
        config = AgentConfig(llm=LLMConfig(provider="openai", api_key="inline"))
        svc = pf._create_llm_service(config, _creds(PROJECT, openai="platform"))
        assert svc._client.api_key == "inline"
        svc = pf._create_llm_service(AgentConfig(), _creds(PROJECT, openai="platform"))
        assert svc._client.api_key == "project-openai"
        svc = pf._create_llm_service(AgentConfig(), _creds(openai="platform"))
        assert svc._client.api_key == "platform"

    @pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
    def test_inline_then_project_then_platform(self, provider) -> None:
        kw = {"model": "m"}
        inline = AgentConfig(llm=LLMConfig(provider=provider, api_key="inline", **kw))
        bare = AgentConfig(llm=LLMConfig(provider=provider, **kw))
        both = _creds(PROJECT, **{provider: "platform"})
        assert pf._create_llm_service(inline, both)._client.api_key == "inline"
        assert (
            pf._create_llm_service(bare, both)._client.api_key == f"project-{provider}"
        )
        only = _creds(**{provider: "platform"})
        assert pf._create_llm_service(bare, only)._client.api_key == "platform"

    def test_custom_openai_takes_only_its_inline_key(self) -> None:
        config = AgentConfig(
            llm=LLMConfig(
                provider="custom_openai", base_url="https://x.test/v1", model="m"
            )
        )
        svc = pf._create_llm_service(config, _creds(PROJECT, openai="platform"))
        assert svc._client.api_key == "no-key"

    def test_bedrock_resolves_the_project_aws_set(self) -> None:
        config = AgentConfig(llm=LLMConfig(provider="bedrock", model="meta.llama"))
        creds = _creds(
            {
                "aws": {
                    "access_key_id": "AKIAPROJECT",
                    "secret_access_key": "proj-secret",
                    "region": "eu-central-1",
                }
            }
        )
        params = pf._create_llm_service(config, creds)._aws_params
        assert params["aws_access_key_id"] == "AKIAPROJECT"
        assert params["region_name"] == "eu-central-1"

    def test_nova_sonic_resolves_the_project_aws_set(self) -> None:
        config = AgentConfig(pipeline_mode="s2s", s2s=S2SConfig(provider="aws"))
        creds = _creds({"aws": {"access_key_id": "AKIANOVA", "secret_access_key": "n"}})
        assert create_s2s_service(config, creds)._access_key_id == "AKIANOVA"

    @pytest.mark.parametrize(
        ("provider", "account", "module", "cls"),
        [
            (
                "openai",
                "openai",
                "pipecat.services.openai.realtime.llm",
                "OpenAIRealtimeLLMService",
            ),
            (
                "google",
                "google",
                "pipecat.services.google.gemini_live.llm",
                "GeminiLiveLLMService",
            ),
        ],
    )
    def test_s2s(self, monkeypatch, provider, account, module, cls) -> None:
        mock = _ctor(monkeypatch, module, cls)
        config = AgentConfig(pipeline_mode="s2s", s2s=S2SConfig(provider=provider))
        create_s2s_service(config, _creds(PROJECT, **{account: "platform"}))
        assert _key(mock) == f"project-{account}"

    def test_openai_live_bills_to_the_openai_account(self) -> None:
        config = AgentConfig(pipeline_mode="s2s", s2s=S2SConfig(provider="openai_live"))
        svc = create_s2s_service(config, _creds(PROJECT, openai="platform"))
        assert svc.api_key == "project-openai"

    @pytest.mark.parametrize(
        ("provider", "module", "cls", "extra"),
        [
            (
                "heygen",
                "pipecat.services.heygen.video",
                "HeyGenVideoService",
                {"avatar_id": "a1"},
            ),
            (
                "tavus",
                "pipecat.services.tavus.video",
                "TavusVideoService",
                {"replica_id": "r1"},
            ),
        ],
    )
    def test_avatar(self, monkeypatch, provider, module, cls, extra) -> None:
        mock = _ctor(monkeypatch, module, cls)
        avatar = AvatarConfig(enabled=True, provider=provider, **extra)
        pf._create_avatar_service(avatar, _creds(PROJECT, **{provider: "platform"}))
        assert _key(mock) == f"project-{provider}"
        assert pf._create_avatar_service(avatar, _creds()) is None


@pytest.mark.asyncio
class TestTheCallReadsItsProject:
    async def test_a_written_credential_is_what_the_next_call_runs_on(self) -> None:
        from turncall.orchestrator.pipeline_builder import load_call_credentials

        session = AsyncMock()

        class _Factory:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *a):
                return False

        settings = Settings()
        load = AsyncMock(return_value={"deepgram": {"api_key": "first"}})
        with patch(f"{REPO}.load_documents", load):
            first = await load_call_credentials(_Factory, uuid.uuid4(), settings)
            load.return_value = {}
            second = await load_call_credentials(_Factory, uuid.uuid4(), settings)
        assert first.api_key("deepgram") == "first"
        # Deleted between calls: the next one is back on the platform key.
        assert second.api_key("deepgram") == settings.deepgram.api_key


def _settings_with_key(key: str = "") -> SimpleNamespace:
    return SimpleNamespace(credentials=SimpleNamespace(encryption_key=key))


class TestEncryption:
    def test_round_trip(self) -> None:
        s = _settings_with_key(Fernet.generate_key().decode())
        token = credential_repo.encrypt({"api_key": SECRET}, s)
        assert SECRET not in token
        assert credential_repo.decrypt(token, s) == {"api_key": SECRET}

    def test_writing_without_a_key_fails_closed(self) -> None:
        with pytest.raises(credential_repo.CredentialKeyError, match="not set"):
            credential_repo.encrypt({"api_key": "x"}, _settings_with_key())

    def test_another_key_cannot_read_it(self) -> None:
        token = credential_repo.encrypt(
            {"api_key": "x"}, _settings_with_key(Fernet.generate_key().decode())
        )
        other = _settings_with_key(Fernet.generate_key().decode())
        with pytest.raises(credential_repo.CredentialKeyError, match="rotated"):
            credential_repo.decrypt(token, other)

    @pytest.mark.asyncio
    async def test_startup_fails_when_credentials_exist_and_key_is_unset(
        self,
    ) -> None:
        token = credential_repo.encrypt(
            {"api_key": "x"}, _settings_with_key(Fernet.generate_key().decode())
        )
        session = AsyncMock()
        session.scalar.return_value = token
        with pytest.raises(credential_repo.CredentialKeyError):
            await credential_repo.ensure_readable(session, _settings_with_key())

    @pytest.mark.asyncio
    async def test_startup_without_credentials_needs_no_key(self) -> None:
        session = AsyncMock()
        session.scalar.return_value = None
        await credential_repo.ensure_readable(session, _settings_with_key())


@pytest.fixture
def project_id(app):
    pid = uuid.uuid4()
    ctx = AuthContext(
        project_id=pid, api_key_id=uuid.uuid4(), role=ProjectRole.DEVELOPER
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    return pid


def _row(provider: str) -> SimpleNamespace:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 1, tzinfo=UTC)
    return SimpleNamespace(
        provider=provider, ciphertext="gAAAA-cipher", created_at=now, updated_at=now
    )


class TestSchemaBoundary:
    def test_unknown_provider_is_named(self, app, project_id) -> None:
        r = app.put("/v1/credentials/ollama", headers=H, json={"api_key": "k"})
        assert r.status_code == 422
        assert "'ollama'" in r.json()["error"]

    @pytest.mark.parametrize(
        "body",
        [{}, {"api_key": "   "}, {"api_key": "k", "extra": 1}, {"key": "k"}],
    )
    def test_malformed_single_key(self, app, project_id, body) -> None:
        r = app.put("/v1/credentials/deepgram", headers=H, json=body)
        assert r.status_code == 422, r.text
        assert r.json()["error"].startswith("Invalid deepgram credential")

    @pytest.mark.parametrize(
        "body",
        [
            {"region": "us-east-1"},  # names no source
            {"access_key_id": "AKIA"},  # half a pair
            {"api_key": "k"},  # wrong shape for aws
        ],
    )
    def test_malformed_aws_set(self, app, project_id, body) -> None:
        r = app.put("/v1/credentials/aws", headers=H, json=body)
        assert r.status_code == 422, r.text

    def test_inline_static_aws_keys_still_rejected_with_flag_off(self) -> None:
        assert not get_settings().aws.agent_credentials_enabled
        with pytest.raises(ValidationError, match="PUT /v1/credentials/aws"):
            AgentConfigSchema.model_validate(
                {"aws": {"access_key_id": "AKIA", "secret_access_key": "s"}}
            )

    def test_project_aws_set_accepted_with_flag_off(self, app, project_id) -> None:
        assert not get_settings().aws.agent_credentials_enabled
        upsert = AsyncMock(return_value=_row("aws"))
        with patch(f"{REPO}.upsert", upsert):
            r = app.put(
                "/v1/credentials/aws",
                headers=H,
                json={
                    "access_key_id": "AKIA",
                    "secret_access_key": "s",
                    "region": "eu-west-1",
                },
            )
        assert r.status_code == 200, r.text
        assert upsert.await_args.kwargs["document"] == {
            "access_key_id": "AKIA",
            "secret_access_key": "s",
            "region": "eu-west-1",
        }

    def test_write_is_scoped_to_the_callers_project(self, app, project_id) -> None:
        upsert = AsyncMock(return_value=_row("deepgram"))
        with patch(f"{REPO}.upsert", upsert):
            r = app.put("/v1/credentials/deepgram", headers=H, json={"api_key": " k "})
        assert r.status_code == 200
        kw = upsert.await_args.kwargs
        assert kw["project_id"] == project_id
        assert kw["document"] == {"api_key": "k"}

    def test_delete_is_idempotent(self, app, project_id) -> None:
        with patch(f"{REPO}.delete_credential", AsyncMock(return_value=False)) as d:
            r = app.delete("/v1/credentials/deepgram", headers=H)
        assert r.status_code == 200
        assert r.json()["data"] == {"provider": "deepgram", "deleted": False}
        assert d.await_args.args[1] == project_id


class TestFailsClosed:
    def test_a_write_without_the_key_says_which_setting(
        self, app, project_id, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "turncall.api.v1.credentials.get_settings",
            lambda: _settings_with_key(),
        )
        r = app.put("/v1/credentials/deepgram", headers=H, json={"api_key": "k"})
        assert r.status_code == 503
        assert "CREDENTIAL_ENCRYPTION_KEY" in r.json()["error"]


class TestSecretMasking:
    def test_write_response_carries_no_value(self, app, project_id) -> None:
        with patch(f"{REPO}.upsert", AsyncMock(return_value=_row("openai"))):
            r = app.put("/v1/credentials/openai", headers=H, json={"api_key": SECRET})
        assert SECRET not in r.text
        assert "cipher" not in r.text
        assert r.json()["data"]["present"] is True

    def test_list_carries_presence_only(self, app, project_id) -> None:
        with patch(
            f"{REPO}.list_for_project", AsyncMock(return_value=[_row("openai")])
        ):
            r = app.get("/v1/credentials", headers=H)
        assert r.json()["data"][0].keys() == {
            "provider",
            "present",
            "created_at",
            "updated_at",
        }
        assert "cipher" not in r.text

    def test_providers_reports_two_independent_signals(
        self, app, project_id, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "turncall.api.v1.credentials.platform_keys",
            lambda s: {"deepgram": "env-key", "openai": ""},
        )
        monkeypatch.setattr(
            "turncall.api.v1.credentials.ambient_aws_available", lambda: False
        )
        with patch(
            f"{REPO}.providers_for_project",
            AsyncMock(return_value={"deepgram", "openai"}),
        ) as held:
            r = app.get("/v1/providers", headers=H)
        assert r.status_code == 200
        assert "env-key" not in r.text
        assert held.await_args.args[1] == project_id
        data = r.json()["data"]
        assert data["roles"] == {k: list(v) for k, v in ROLE_PROVIDERS.items()}
        acc = data["accounts"]
        assert acc["deepgram"]["platform_key"] is True
        assert acc["deepgram"]["project_credential"] is True
        assert acc["openai"]["platform_key"] is False
        assert acc["openai"]["project_credential"] is True
        assert acc["cartesia"]["project_credential"] is False
        assert acc["aws"] == {
            "serves": {"llm": ["bedrock"], "s2s": ["aws"]},
            "platform_key": False,
            "project_credential": False,
        }

    def test_validator_lists_are_the_endpoints_lists(self) -> None:
        for provider in ROLE_PROVIDERS["stt"]:
            AgentConfigSchema.model_validate({"stt": {"provider": provider}})
        with pytest.raises(ValidationError):
            AgentConfigSchema.model_validate({"stt": {"provider": "whisperx"}})
