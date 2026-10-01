"""Which provider key a call runs on (ADR-0020, #161/#162/#164).

The one resolution path for every provider that takes a credential. Narrowest
wins — the ordering the eval judge already uses:

    agent config (llm.api_key, config.aws)
      -> project credential
        -> platform environment

`Credentials` is built once per call, before the (synchronous) pipeline
factory runs, so a credential written between two calls takes effect on the
next one with no restart.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from turncall.domain.providers import AWS_ACCOUNT

if TYPE_CHECKING:
    from turncall.config.settings import Settings
    from turncall.domain.models import AWSConfig


def platform_keys(settings: Settings) -> dict[str, str]:
    """The platform environment's key per credential account."""
    return {
        "openai": settings.openai.api_key,
        "anthropic": settings.anthropic.api_key,
        "openrouter": settings.openrouter.api_key,
        "google": settings.google.api_key,
        "deepgram": settings.deepgram.api_key,
        "elevenlabs": settings.elevenlabs.api_key,
        "cartesia": settings.cartesia.api_key,
        "heygen": settings.heygen.api_key,
        "tavus": settings.tavus.api_key,
    }


# ponytail: cached for the process; a host that gains or loses ambient
# credentials after start is reported as it was at first ask.
@functools.cache
def ambient_aws_available() -> bool:
    """Whether boto3's ambient chain yields credentials on this host.

    The platform level for the AWS account: there is no single env var, since
    an instance profile, IRSA or an SSO cache all count.
    """
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def _has_aws_source(aws: AWSConfig) -> bool:
    return bool(aws.access_key_id or aws.role_arn or aws.profile)


@dataclass(frozen=True)
class Credentials:
    """Every level of key a call can resolve, ready before the pipeline builds."""

    platform: Mapping[str, str] = field(default_factory=dict)
    # account -> decrypted document ({"api_key": ...} or an AWS credential set)
    project: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        project: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Credentials:
        return cls(platform=platform_keys(settings), project=project or {})

    def api_key(self, account: str, inline: str | None = None) -> str:
        """The key for a single-key account; "" when no level holds one."""
        if inline:
            return inline
        stored = self.project.get(account, {}).get("api_key")
        return stored or self.platform.get(account, "")

    def aws(self, inline: AWSConfig) -> AWSConfig:
        """The AWS block to resolve from.

        An agent block naming any credential source wins whole. Otherwise the
        project's set is used, keeping the agent's region when it names one —
        Bedrock model availability is regional and an agent's choice. Neither
        means the ambient chain, exactly as before.
        """
        stored = self.project.get(AWS_ACCOUNT)
        if _has_aws_source(inline) or not stored:
            return inline
        merged = {**stored, "region": inline.region or stored.get("region")}
        return inline.model_validate(merged)
