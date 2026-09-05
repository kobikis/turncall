"""Central AWS credential resolution for the bedrock/aws providers (ADR-0016).

Pipecat's two AWS services disagree about credentials. ``AWSBedrockLLMService``
takes ``aws_access_key: str | None = None`` and falls back to boto3's default
chain; ``AWSNovaSonicLLMService`` requires an explicit ``access_key_id`` /
``secret_access_key`` / ``region`` with no fallback at all.

Deferring to boto3 would therefore make one agent config behave differently by
pipeline mode — an agent relying on an instance profile works in cascade and
hard-fails the moment ``pipeline_mode`` is set to ``s2s``. So everything is
resolved here instead, and both services are handed an explicit frozen tuple.
SSO, instance profiles, IRSA, ECS task roles and assume-role all collapse into
this one path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from turncall.config.settings import Settings, get_settings

if TYPE_CHECKING:
    from turncall.domain.models import AWSConfig

_SESSION_NAME = "turncall"


class AWSCredentialsError(RuntimeError):
    """No usable AWS credentials could be resolved for this agent."""


@dataclass(frozen=True)
class AWSCredentials:
    """An explicit, frozen credential tuple ready to hand to an AWS service."""

    access_key_id: str
    secret_access_key: str
    session_token: str | None
    region: str

    def bedrock_kwargs(self) -> dict[str, Any]:
        """Kwargs for AWSBedrockLLMService, which uses aws_-prefixed names."""
        return {
            "aws_access_key": self.access_key_id,
            "aws_secret_key": self.secret_access_key,
            "aws_session_token": self.session_token,
            "aws_region": self.region,
        }

    def nova_sonic_kwargs(self) -> dict[str, Any]:
        """Kwargs for AWSNovaSonicLLMService, which uses unprefixed names.

        The two services spell the same four values differently; keeping both
        spellings here stops that inconsistency leaking into agent config.
        """
        return {
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "session_token": self.session_token,
            "region": self.region,
        }


def _base_session(aws: AWSConfig, region: str) -> Any:
    """Build the session credentials are read from, before any assume-role."""
    import boto3

    if aws.access_key_id and aws.secret_access_key:
        return boto3.Session(
            aws_access_key_id=aws.access_key_id,
            aws_secret_access_key=aws.secret_access_key,
            aws_session_token=aws.session_token,
            region_name=region,
        )
    if aws.profile:
        # How an SSO login is selected. Note this reads a *cached* token: the
        # operator must have run `aws sso login`, and it expires. Servers should
        # use an instance profile, IRSA, or role_arn instead.
        return boto3.Session(profile_name=aws.profile, region_name=region)
    return boto3.Session(region_name=region)


def _assume_role(session: Any, aws: AWSConfig, region: str) -> Any:
    """Exchange the base session for temporary credentials on aws.role_arn."""
    import boto3

    # Blocking network call (~100ms) on the call-setup path. Acceptable at
    # pipeline build; if it ever shows up in call-start latency, move it to a
    # thread and cache per (role_arn, region) until shortly before expiry.
    sts = session.client("sts", region_name=region)
    kwargs: dict[str, Any] = {
        "RoleArn": aws.role_arn,
        "RoleSessionName": _SESSION_NAME,
    }
    if aws.external_id:
        kwargs["ExternalId"] = aws.external_id

    creds = sts.assume_role(**kwargs)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def resolve_aws_credentials(
    aws: AWSConfig,
    *,
    settings: Settings | None = None,
) -> AWSCredentials:
    """Resolve an explicit credential tuple from whichever source applies.

    Call this again on Nova Sonic's session rollover rather than caching the
    result: assume-role and IRSA credentials are typically valid for about an
    hour, and a call outliving them would otherwise die mid-conversation.

    Raises:
        AWSCredentialsError: when no source yields usable credentials.
    """
    settings = settings or get_settings()
    region = aws.region or settings.aws.region

    session = _base_session(aws, region)
    if aws.role_arn:
        session = _assume_role(session, aws, region)

    credentials = session.get_credentials()
    if credentials is None:
        raise AWSCredentialsError(
            "No AWS credentials found for region "
            f"{region!r}. Set aws.access_key_id/aws.secret_access_key on the "
            "agent, an aws.role_arn to assume, or make credentials available "
            "to the process (env vars, an SSO profile, an instance profile or "
            "IRSA)."
        )

    frozen = credentials.get_frozen_credentials()
    logger.debug(
        "aws_credentials_resolved",
        region=region,
        source=(
            "role_arn"
            if aws.role_arn
            else "static"
            if aws.access_key_id
            else "profile"
            if aws.profile
            else "ambient"
        ),
        temporary=frozen.token is not None,
    )
    return AWSCredentials(
        access_key_id=frozen.access_key,
        secret_access_key=frozen.secret_key,
        session_token=frozen.token,
        region=region,
    )
