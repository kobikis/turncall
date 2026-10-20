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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from turncall.domain.errors import PicklableError
from turncall.domain.models import resolve_delegation
from turncall.domain.providers import AWS_ACCOUNT, account_for

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.config.settings import Settings
    from turncall.domain.models import AgentConfig, AWSConfig


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
        "assemblyai": settings.assemblyai.api_key,
        "xai": settings.xai.api_key,
        "heygen": settings.heygen.api_key,
        "tavus": settings.tavus.api_key,
        "typesafe": settings.typesafe.api_key,
        "telnyx": settings.telnyx.api_key,
        # Not a key: an Asterisk account is a URL and a login (#209). Present
        # when the platform has one, which is all `GET /v1/providers` reports.
        "asterisk": settings.asterisk.ari_url,
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
        logger.exception("ambient_aws_probe_failed")
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

    def missing_keys(
        self, config: AgentConfig, *, has_knowledge_tool: bool = False
    ) -> list[str]:
        """The accounts this agent needs and no level holds a key for.

        Without one the provider refuses mid-call — or the SDK refuses at build
        — and either way the caller hears nothing. AWS is not checked: its
        ambient chain is not a key this can see. `has_knowledge_tool`: the agent
        has a tool-mode KB, which lives outside the config (#254).
        """
        needed: list[tuple[str, str | None]]
        if config.pipeline_mode == "s2s":
            needed = [(config.s2s.provider, None)]
            # A client delegate is the agent's own llm (#244). MCP servers
            # count as tools before discovery, so a keyless delegate is caught
            # whether or not they answer; so does `query_knowledge`.
            delegation = resolve_delegation(
                config.s2s,
                has_tools=bool(
                    config.tools or config.mcp_servers or has_knowledge_tool
                ),
            )
            if delegation is not None and delegation.mode == "client":
                needed.append((config.llm.provider, config.llm.api_key))
                needed.extend((e.provider, e.api_key) for e in config.llm.failover)
        else:
            needed = [
                (config.stt.provider, None),
                (config.llm.provider, config.llm.api_key),
                (config.tts.provider, None),
                # A backup with no key would only be found out mid-outage.
                *((e.provider, e.api_key) for e in config.llm.failover),
                *((e.provider, None) for e in config.stt.failover),
                *((e.provider, None) for e in config.tts.failover),
            ]
            if config.avatar.enabled:
                needed.append((config.avatar.provider, None))
        missing: list[str] = []
        for provider, inline in needed:
            account = account_for(provider)
            if account in (None, AWS_ACCOUNT) or account in missing:
                continue
            if not self.api_key(account, inline):
                missing.append(account)
        return missing


class MissingProviderKeyError(PicklableError, RuntimeError):
    """An agent names a provider no level holds a key for (ADR-0020)."""

    def __init__(self, accounts: list[str]) -> None:
        self.accounts = accounts
        super().__init__(
            f"No API key for {', '.join(accounts)}: add it as a project "
            "credential or set it in the platform environment"
        )


async def for_project(
    session: AsyncSession, project_id: UUID, settings: Settings | None = None
) -> Credentials:
    """Every level of key for work done on one project's behalf — a call, a
    chat turn, a post-call analysis, a document upload, an eval run. Read fresh
    each time, so a credential written a moment ago is the one used."""
    from turncall.config.settings import get_settings
    from turncall.storage.repositories import credential_repo

    settings = settings or get_settings()
    project = await credential_repo.load_documents(session, project_id, settings)
    return Credentials.from_settings(settings, project)


# Only for code that cannot be handed `Credentials` as an argument: the eval
# judge and persona, which pipecat constructs from a factory path inside the run.
_current: ContextVar[Credentials | None] = ContextVar(
    "turncall_credentials", default=None
)


def current(settings: Settings | None = None) -> Credentials:
    """The credentials of the project whose work is running, else the platform's."""
    found = _current.get()
    if found is not None:
        return found
    from turncall.config.settings import get_settings

    return Credentials.from_settings(settings or get_settings())


@contextmanager
def scope(credentials: Credentials) -> Iterator[None]:
    """Make `credentials` what `current()` answers inside this block (and in
    every task started from it — asyncio copies the context)."""
    token = _current.set(credentials)
    try:
        yield
    finally:
        _current.reset(token)
