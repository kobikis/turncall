"""Which Asterisk a project's calls run on (#209, ADR-0020).

A project's own `asterisk` credential, else the platform's `ASTERISK_*` one.
One Asterisk per project: a project with its own is never served by the
platform's, so a call's REST actions always reach the PBX that rang it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from turncall.adapters.telephony.asterisk_adapter import AriAccount
from turncall.storage.repositories import credential_repo

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.config.settings import Settings

ASTERISK_ACCOUNT = "asterisk"


def platform_account(settings: Settings) -> AriAccount | None:
    a = settings.asterisk
    if not a.ari_url:
        return None
    return AriAccount(
        url=a.ari_url.rstrip("/"),
        username=a.ari_username,
        password=a.ari_password,
        app=a.app,
        media_connection=a.media_connection,
        transfer_context=a.transfer_context,
    )


def from_credential(document: Mapping[str, Any]) -> AriAccount:
    """A stored `asterisk` credential (validated at write) as an account."""
    return AriAccount(
        url=document["ari_url"],
        username=document["username"],
        password=document["password"],
        app=document.get("app", "turncall"),
        media_connection=document.get("media_connection", "turncall"),
        transfer_context=document.get("transfer_context", "turncall-transfer"),
    )


async def account_for_project(
    session: AsyncSession, project_id: UUID, settings: Settings
) -> AriAccount | None:
    documents = await credential_repo.load_documents(session, project_id, settings)
    own = documents.get(ASTERISK_ACCOUNT)
    return from_credential(own) if own else platform_account(settings)


async def all_accounts(
    session: AsyncSession, settings: Settings
) -> dict[UUID | None, AriAccount]:
    """Every Asterisk this deployment serves: project -> its own, None -> the
    platform's. What the inbound edge keeps an ARI connection open to."""
    stored = await credential_repo.documents_for_provider(
        session, ASTERISK_ACCOUNT, settings
    )
    accounts: dict[UUID | None, AriAccount] = {
        project_id: from_credential(doc) for project_id, doc in stored.items()
    }
    platform = platform_account(settings)
    if platform is not None:
        accounts[None] = platform
    return accounts
