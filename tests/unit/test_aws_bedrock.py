"""AWS Bedrock LLM + Nova Sonic S2S support (ADR-0016).

The tests that matter most here assert that resolved credentials and region
actually *arrive at the service*, for each credential source. Every other
provider is a single API key, so this is the first config that can plausibly be
resolved, logged, and then silently dropped on the floor — which is the failure
mode ADR-0014 was written about.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import (
    AgentConfigSchema,
    AWSConfigSchema,
    LLMConfigSchema,
    S2SConfigSchema,
    _sanitize_config,
)
from turncall.config.settings import get_settings
from turncall.domain.models import AgentConfig, AWSConfig, LLMConfig, S2SConfig
from turncall.services import aws_credentials
from turncall.services.aws_credentials import (
    AWSCredentialsError,
    resolve_aws_credentials,
)

_STATIC = AWSConfig(access_key_id="AKIATEST", secret_access_key="shh")


@pytest.fixture
def allow_agent_keys(monkeypatch: pytest.MonkeyPatch):
    """Enable per-agent static keys, which are off by default."""
    monkeypatch.setenv("AWS_AGENT_CREDENTIALS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.unit
class TestCredentialResolution:
    def test_static_keys_resolve_verbatim(self) -> None:
        creds = resolve_aws_credentials(
            AWSConfig(
                access_key_id="AKIA1",
                secret_access_key="secret1",
                region="ap-south-1",
            )
        )
        assert creds.access_key_id == "AKIA1"
        assert creds.secret_access_key == "secret1"
        assert creds.region == "ap-south-1"
        assert creds.session_token is None

    def test_region_falls_back_to_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        get_settings.cache_clear()
        try:
            assert resolve_aws_credentials(_STATIC).region == "eu-central-1"
            # ...and an explicit agent region wins over it. AWS_REGION also
            # points at the S3 bucket, so these must not be welded together.
            explicit = _STATIC.model_copy(update={"region": "us-west-1"})
            assert resolve_aws_credentials(explicit).region == "us-west-1"
        finally:
            get_settings.cache_clear()

    def test_role_arn_exchanges_for_temporary_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "ASIATEMP",
                "SecretAccessKey": "tempsecret",
                "SessionToken": "session-token",
            }
        }
        session = MagicMock()
        session.client.return_value = sts
        monkeypatch.setattr(
            aws_credentials, "_base_session", lambda aws, region: session
        )

        creds = resolve_aws_credentials(
            AWSConfig(
                role_arn="arn:aws:iam::123456789012:role/turncall",
                external_id="ext-1",
                region="eu-west-2",
            )
        )

        assert creds.access_key_id == "ASIATEMP"
        assert creds.session_token == "session-token"
        assert creds.region == "eu-west-2"
        kwargs = sts.assume_role.call_args.kwargs
        assert kwargs["RoleArn"] == "arn:aws:iam::123456789012:role/turncall"
        assert kwargs["ExternalId"] == "ext-1"

    def test_no_credentials_raises_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.get_credentials.return_value = None
        monkeypatch.setattr(
            aws_credentials, "_base_session", lambda aws, region: session
        )
        with pytest.raises(AWSCredentialsError, match="No AWS credentials"):
            resolve_aws_credentials(AWSConfig(region="us-east-1"))


@pytest.mark.unit
class TestCredentialsReachTheServices:
    """The point of the exercise: config that resolves but never arrives is
    indistinguishable from config that was never set."""

    def test_bedrock_llm_receives_credentials_and_settings(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        config = AgentConfig(
            llm=LLMConfig(
                provider="bedrock",
                model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                temperature=0.25,
                max_tokens=999,
                extra={"thinking": {"type": "enabled"}},
            ),
            aws=AWSConfig(
                access_key_id="AKIABEDROCK",
                secret_access_key="bedrock-secret",
                region="ap-southeast-2",
            ),
        )
        service = _create_llm_service(config, "unused-openai-key")

        params = service._aws_params
        assert params["aws_access_key_id"] == "AKIABEDROCK"
        assert params["aws_secret_access_key"] == "bedrock-secret"
        assert params["region_name"] == "ap-southeast-2"

        settings = service._settings
        assert settings.model == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert settings.temperature == 0.25
        assert settings.max_tokens == 999
        # llm.extra is Bedrock's passthrough — how Anthropic extended thinking
        # is reached, rather than growing a second reasoning_effort spelling.
        assert settings.additional_model_request_fields == {
            "thinking": {"type": "enabled"}
        }

    def test_nova_sonic_receives_credentials_and_own_defaults(self) -> None:
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(provider="aws"),
            aws=AWSConfig(
                access_key_id="AKIANOVA",
                secret_access_key="nova-secret",
                region="us-west-2",
            ),
            system_prompt="Be brief.",
        )
        service = create_s2s_service(config, "unused")

        assert service._access_key_id == "AKIANOVA"
        assert service._secret_access_key == "nova-secret"
        assert service._region == "us-west-2"
        # S2SConfig's defaults are OpenAI Realtime's; Nova Sonic needs its own.
        assert service._settings.model == "amazon.nova-2-sonic-v1:0"
        assert service._settings.voice == "matthew"
        assert service._settings.system_instruction == "Be brief."

    def test_nova_sonic_keeps_an_explicit_model_and_voice(self) -> None:
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(
                provider="aws", model="amazon.nova-sonic-v1:0", voice="tiffany"
            ),
            aws=_STATIC,
        )
        service = create_s2s_service(config, "unused")
        assert service._settings.model == "amazon.nova-sonic-v1:0"
        assert service._settings.voice == "tiffany"

    def test_endpointing_sensitivity_rides_in_extra(self) -> None:
        """turn_detection's server_vad|pipecat_vad enum has no room for it, so
        it goes through extra. Nova Sonic 2 only — Pipecat nulls it on v1."""
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(provider="aws", extra={"endpointing_sensitivity": "HIGH"}),
            aws=_STATIC,
        )
        service = create_s2s_service(config, "unused")
        assert service._settings.endpointing_sensitivity == "HIGH"

    def test_create_client_re_resolves_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nova Sonic rolls sessions over at ~6 min and calls create_client()
        again. Temporary credentials outlive neither a long call nor that
        rollover unless they are refreshed here."""
        from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService

        from turncall.orchestrator import s2s_config

        config = AgentConfig(
            pipeline_mode="s2s", s2s=S2SConfig(provider="aws"), aws=_STATIC
        )
        service = s2s_config.create_s2s_service(config, "unused")
        assert service._access_key_id == "AKIATEST"

        # Don't build a real client; we only care that credentials refreshed.
        monkeypatch.setattr(
            AWSNovaSonicLLMService, "create_client", lambda self: "client"
        )
        rotated = aws_credentials.AWSCredentials(
            access_key_id="ASIAROTATED",
            secret_access_key="rotated",
            session_token="fresh-token",
            region="eu-west-1",
        )
        monkeypatch.setattr(
            aws_credentials, "resolve_aws_credentials", lambda aws: rotated
        )

        assert service.create_client() == "client"
        assert service._access_key_id == "ASIAROTATED"
        assert service._session_token == "fresh-token"
        assert service._region == "eu-west-1"


@pytest.mark.unit
class TestSchemaBoundary:
    def test_bedrock_is_an_accepted_llm_provider(self) -> None:
        schema = LLMConfigSchema(provider="bedrock", model="amazon.nova-pro-v1:0")
        assert schema.provider == "bedrock"

    def test_aws_is_an_accepted_s2s_provider(self) -> None:
        assert S2SConfigSchema(provider="aws").provider == "aws"

    def test_base_url_is_rejected_not_ignored(self) -> None:
        # Bedrock is reached through boto3, so a base_url would silently do
        # nothing — the exact shape of bug this ADR exists to avoid.
        with pytest.raises(ValidationError, match="base_url is not supported"):
            LLMConfigSchema(provider="bedrock", base_url="https://example.com")
        with pytest.raises(ValidationError, match="base_url is not supported"):
            S2SConfigSchema(provider="aws", base_url="wss://example.com")

    def test_partial_static_keys_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be set together"):
            AWSConfigSchema(access_key_id="AKIA")

    def test_static_keys_rejected_when_flag_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_AGENT_CREDENTIALS_ENABLED", "false")
        get_settings.cache_clear()
        try:
            with pytest.raises(ValidationError, match="static AWS keys are disabled"):
                AWSConfigSchema(access_key_id="AKIA", secret_access_key="shh")
        finally:
            get_settings.cache_clear()

    def test_role_arn_needs_no_flag(self) -> None:
        """The multi-tenant path stores no durable secret, so it is never gated."""
        schema = AWSConfigSchema(role_arn="arn:aws:iam::123456789012:role/turncall")
        assert schema.role_arn.endswith("role/turncall")

    def test_static_keys_allowed_when_flag_enabled(self, allow_agent_keys) -> None:
        schema = AgentConfigSchema(
            llm={"provider": "bedrock", "model": "amazon.nova-pro-v1:0"},
            aws={"access_key_id": "AKIA", "secret_access_key": "shh"},
        )
        assert schema.aws.access_key_id == "AKIA"


@pytest.mark.unit
class TestSecretMasking:
    def test_aws_secrets_are_masked_on_read(self, allow_agent_keys) -> None:
        config = AgentConfigSchema(
            aws={
                "access_key_id": "AKIA",
                "secret_access_key": "shh",
                "session_token": "tok",
                "region": "us-east-1",
            }
        ).model_dump()

        masked = _sanitize_config(config)

        assert masked["aws"]["secret_access_key"] == "***"
        assert masked["aws"]["session_token"] == "***"
        # Not secrets: an access key id and region are needed to debug a config.
        assert masked["aws"]["access_key_id"] == "AKIA"
        assert masked["aws"]["region"] == "us-east-1"

    def test_unset_secrets_stay_none(self) -> None:
        masked = _sanitize_config(AgentConfigSchema().model_dump())
        assert masked["aws"]["secret_access_key"] is None
