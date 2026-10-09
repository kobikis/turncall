"""Which Telnyx account a project's calls run on (ADR-0022, ADR-0020).

A project's `telnyx` credential — its API key and the public key its account
signs webhooks with — replaces the platform's TELNYX_API_KEY /
TELNYX_PUBLIC_KEY whole: the project's own account is what sends its numbers'
webhooks, so its key must be the one that verifies them. Every Telnyx command
and every Telnyx signature check resolves its account here, and `adapter` is
the only place a Telnyx adapter is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turncall.domain.carriers import TELNYX
from turncall.storage.repositories import credential_repo

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.config.settings import Settings


@dataclass(frozen=True)
class TelnyxAccount:
    """`api_key` / `public_key` are "" when no level holds one."""

    api_key: str
    public_key: str

    def adapter(self) -> Any:
        from turncall.adapters.telephony.telnyx_adapter import TelnyxAdapter

        return TelnyxAdapter(self.api_key)


def platform_account(settings: Settings) -> TelnyxAccount:
    return TelnyxAccount(settings.telnyx.api_key, settings.telnyx.public_key)


async def account_for_project(
    session: AsyncSession, project_id: UUID | None, settings: Settings
) -> TelnyxAccount:
    """The project's own Telnyx account if it holds one, else the platform's."""
    if project_id is not None:
        documents = await credential_repo.load_documents(session, project_id, settings)
        own = documents.get(TELNYX)
        if own:
            return TelnyxAccount(own.get("api_key", ""), own.get("public_key", ""))
    return platform_account(settings)
