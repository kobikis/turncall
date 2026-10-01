"""Project credentials and provider capabilities (ADR-0020, #162/#163/#164).

A project's own provider keys: written here, encrypted at rest, resolved at
pipeline build ahead of the platform environment. No read returns a value.
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Body

from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ErrorCode
from turncall.api.responses import ok
from turncall.api.v1.schemas.credentials import (
    CredentialResponse,
    parse_credential,
    require_known_provider,
)
from turncall.auth import Auth, WriteAuth
from turncall.config.settings import get_settings
from turncall.domain.providers import AWS_ACCOUNT, ROLE_PROVIDERS, accounts
from turncall.services.credentials import ambient_aws_available, platform_keys
from turncall.storage.repositories import credential_repo

router = APIRouter(prefix="/credentials", tags=["credentials"])
providers_router = APIRouter(prefix="/providers", tags=["credentials"])


@router.put("/{provider}")
async def put_credential(
    provider: str,
    auth: WriteAuth,
    session: DbSession,
    body: dict[str, Any] = Body(...),  # noqa: B008
) -> dict:
    """Store (or replace) the project's credential for one provider account.

    Takes effect on the project's next call — resolved per call, no restart.
    """
    document = parse_credential(provider, body)
    try:
        row = await credential_repo.upsert(
            session,
            project_id=auth.project_id,
            provider=provider,
            document=document,
            settings=get_settings(),
        )
    except credential_repo.CredentialKeyError as exc:
        # Fail closed, legibly: the operator must set the key, not read a 500.
        raise ApiError(
            status_code=503, code=ErrorCode.CREDENTIAL_KEY_UNAVAILABLE, message=str(exc)
        ) from exc
    await session.commit()
    return ok(CredentialResponse.from_row(row))


@router.get("")
async def list_credentials(auth: Auth, session: DbSession) -> dict:
    rows = await credential_repo.list_for_project(session, auth.project_id)
    return ok([CredentialResponse.from_row(r) for r in rows])


@router.delete("/{provider}")
async def delete_credential(provider: str, auth: WriteAuth, session: DbSession) -> dict:
    """Remove a credential; the project falls back to the platform key.

    Idempotent: deleting one that is not there answers `deleted: false`, so a
    revocation retried under incident pressure never errors.
    """
    require_known_provider(provider)
    deleted = await credential_repo.delete_credential(
        session, auth.project_id, provider
    )
    await session.commit()
    return ok({"provider": provider, "deleted": deleted})


@providers_router.get("")
async def list_providers(auth: Auth, session: DbSession) -> dict:
    """Valid providers per role, and two presence signals per account.

    `platform_key` and `project_credential` stay separate on purpose: together
    they say whether removing the credential falls back or fails (#163).
    """
    held = await credential_repo.providers_for_project(session, auth.project_id)
    platform = {a: bool(k) for a, k in platform_keys(get_settings()).items()}
    # boto3's chain may probe instance metadata on first ask; keep it off the loop.
    platform[AWS_ACCOUNT] = await asyncio.to_thread(lambda: ambient_aws_available())
    return ok(
        {
            "roles": {role: list(p) for role, p in ROLE_PROVIDERS.items()},
            "accounts": {
                account: {
                    "serves": serves,
                    "platform_key": platform.get(account, False),
                    "project_credential": account in held,
                }
                for account, serves in accounts().items()
            },
        }
    )
